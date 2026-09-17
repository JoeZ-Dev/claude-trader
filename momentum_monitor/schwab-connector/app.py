"""
schwab-connector FastAPI app.

The only container that holds the Schwab OAuth token. Owns the stream
subscription and 10s-bar aggregation, and exposes the internal API defined
in specs.md section 5 (internal only -- docker-compose does not publish a
port for this service):

  POST /watch {"symbol": "..."}
  POST /unwatch {"symbol": "..."}
  GET  /bars/{symbol}?since_ts={unix_seconds}  -> array of bar objects
                                                  (specs.md section 4 shape)
  GET  /health  -> {"status": "ok", "watching": [...], "connected": bool}

create_app() takes its dependencies as arguments so tests can inject a
ReplayStreamSource and a temp BarStore. main.py wires the real ones.

`history_fetcher` (optional) is an `async def (symbol) -> list[dict]` that
backfills the current day's bars from Schwab's price-history endpoint (see
price_history.py) before live tick aggregation starts. Without it, a
symbol watched mid-session would have its VWAP/EMA/MACD/level-detection
compute only over bars captured since POST /watch, not over the actual
session -- see price_history.py's module docstring for the full story.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

import json

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from aggregator import BUCKET_SECONDS, BarAggregator
from store import BarStore

logger = logging.getLogger("schwab-connector.backfill")
tick_logger = logging.getLogger("schwab-connector.ticks")

FLUSH_INTERVAL_SECONDS = 2.0
# One heartbeat log line per symbol roughly every 60s (30 flush cycles at
# the 2s default interval) reporting how many REAL ticks arrived in that
# window. Exists because "connected: true" and "no stream errors" are not
# proof real ticks are flowing -- found live 2026-09-17 diagnosing why
# several symbols showed forward-filled, real-market-contradicting flat
# bars: the aggregator/reconnect layers were both healthy and silent, and
# there was no direct way to see whether Schwab was actually delivering
# LEVELONE_EQUITIES ticks for a given symbol at all versus the
# subscription silently never receiving them.
HEARTBEAT_FLUSH_CYCLES = 30
# Bound on each /events subscriber's queue (expected: one subscriber, the
# monitor-app shared consumer). Normal volume is tiny -- one event per
# watched symbol per ~10s bucket -- so this never binds in practice; it
# exists purely to cap worst-case memory if a subscriber stops reading
# entirely, turning a potential leak into a fixed small one.
SUBSCRIBER_QUEUE_MAXSIZE = 1000


class WatchRequest(BaseModel):
    symbol: str


class _DisconnectedSource:
    """Placeholder for a symbol that was asked for but has no working stream
    (e.g. no Schwab token yet)."""
    connected = False


class _TickCounter:
    """A plain mutable box for a real-tick count, shared between _consume's
    tick loop (increments it) and _flush_loop (reads and resets it on its
    own heartbeat cadence) -- simpler than threading a nonlocal through an
    async closure, and gives _flush_loop an object identity to hold onto
    across the whole task's lifetime."""
    def __init__(self) -> None:
        self.count = 0


class Connector:
    """ONE shared stream connection serves every currently-watched symbol
    (fixed 2026-09-17 -- see specs.md: the earlier one-connection-per-
    symbol design put several independent StreamClient logins on the same
    Schwab account concurrently, which throttled badly under real load).
    `source_factory` is now called with a `watched_symbols` GETTER (bound
    method, see `_watched_symbols` below) rather than with no arguments --
    the one shared source reads the CURRENT watched set at (re)connect
    time, not a frozen snapshot. `BarAggregator` stays entirely per-symbol
    (`_aggs`), unaffected by how ticks are delivered to it."""

    def __init__(self, *, store: BarStore, source_factory, replay: bool,
                 now_fn=time.time, flush_interval: float = FLUSH_INTERVAL_SECONDS,
                 history_fetcher=None, heartbeat_flush_cycles: int = HEARTBEAT_FLUSH_CYCLES,
                 subscriber_maxsize: int = SUBSCRIBER_QUEUE_MAXSIZE):
        self._store = store
        self._source_factory = source_factory
        self._replay = replay
        self._now_fn = now_fn
        self._flush_interval = flush_interval
        self._history_fetcher = history_fetcher
        self._heartbeat_flush_cycles = heartbeat_flush_cycles
        self._subscriber_maxsize = subscriber_maxsize
        self._aggs: dict[str, BarAggregator] = {}
        self._tick_counters: dict[str, _TickCounter] = {}
        self._shared_source: object | None = None
        self._consume_task: asyncio.Task | None = None
        self._subscribers: set[asyncio.Queue] = set()

    @property
    def watching(self) -> list[str]:
        return sorted(self._aggs)

    @property
    def connected(self) -> bool:
        return getattr(self._shared_source, "connected", False)

    def _watched_symbols(self) -> set[str]:
        return set(self._aggs)

    def watch(self, symbol: str) -> None:
        key = symbol.upper()
        if key in self._aggs:
            return
        self._aggs[key] = BarAggregator()
        self._tick_counters[key] = _TickCounter()
        asyncio.create_task(self._setup_symbol(key))

    async def unwatch(self, symbol: str) -> bool:
        """Stop live-streaming `symbol`: drops it from `watching` and, if
        the shared connection is live, unsubscribes it immediately.
        Idempotent -- unwatching a symbol that isn't currently watched is
        a no-op returning False. Does NOT touch its stored bars (BarStore)
        -- history stays on disk, only the live subscription stops, same
        as any other restart-safe data in this service. The shared
        connection itself is never torn down here (even to zero symbols)
        -- see _consume_shared."""
        key = symbol.upper()
        if key not in self._aggs:
            return False
        del self._aggs[key]
        self._tick_counters.pop(key, None)
        if self._shared_source is not None:
            remove = getattr(self._shared_source, "remove_symbol", None)
            if remove is not None:
                await remove(key)
        return True

    def bars(self, symbol: str, since_ts: float) -> list[dict]:
        return self._store.since(symbol, since_ts)

    async def shutdown(self) -> None:
        if self._consume_task is not None:
            self._consume_task.cancel()

    # -- internals -------------------------------------------------------

    async def _setup_symbol(self, symbol: str) -> None:
        """Backfill THIS symbol's history before its live bars can append
        (same guarantee _consume used to give per-symbol), then either
        start the one shared consumer (first symbol ever watched) or add
        this symbol to the already-live shared connection."""
        await self._maybe_backfill(symbol)
        if self._consume_task is None:
            self._consume_task = asyncio.create_task(self._consume_shared())
        elif self._shared_source is not None:
            add = getattr(self._shared_source, "add_symbol", None)
            if add is not None:
                await add(symbol)

    async def _consume_shared(self) -> None:
        try:
            source = self._source_factory(self._watched_symbols)
        except Exception:
            # Most likely a missing/expired Schwab token. The service still
            # runs: watched symbols are recorded, /health reports
            # connected=false, and no bars flow until a token is present.
            self._shared_source = _DisconnectedSource()
            return
        self._shared_source = source
        last_tick_ts = 0.0

        flusher = None
        if not self._replay:
            flusher = asyncio.create_task(self._flush_loop())
        try:
            async for symbol, tick in source.ticks():
                agg = self._aggs.get(symbol)
                if agg is None:
                    continue  # unwatched since this tick was sent -- drop
                counter = self._tick_counters.get(symbol)
                if counter is not None:
                    counter.count += 1
                agg.feed(tick)
                last_tick_ts = tick["ts"]
                self._drain(symbol, agg)
            # Stream ended (replay fixture exhausted): close every symbol's
            # last open bucket.
            for symbol, agg in list(self._aggs.items()):
                agg.flush(last_tick_ts + BUCKET_SECONDS)
                self._drain(symbol, agg)
        finally:
            if flusher is not None:
                flusher.cancel()

    async def _maybe_backfill(self, symbol: str) -> None:
        """Fetch and store today's history for a genuinely new symbol,
        before any live bars are appended. Skipped if the store already has
        bars for this symbol (a restart, or a symbol already backfilled) --
        both to avoid a wasted refetch and because re-inserting old bars
        after newer live bars exist would trip the store's monotonic-append
        guard and silently drop those newer bars instead of the redundant
        old ones."""
        if self._history_fetcher is None or self._store.since(symbol, 0.0):
            return
        try:
            bars = await self._history_fetcher(symbol)
        except Exception:
            logger.warning("backfill failed for %s; starting live-only", symbol,
                           exc_info=True)
            return
        if bars:
            self._store.append_many(symbol, bars)
        logger.info("backfilled %d bar(s) for %s", len(bars), symbol)

    async def _flush_loop(self) -> None:
        cycles = 0
        while True:
            await asyncio.sleep(self._flush_interval)
            now = self._now_fn()
            for symbol, agg in list(self._aggs.items()):
                agg.flush(now)
                self._drain(symbol, agg)
            cycles += 1
            if cycles >= self._heartbeat_flush_cycles:
                window_seconds = cycles * self._flush_interval
                for symbol, counter in list(self._tick_counters.items()):
                    tick_logger.info(
                        "event=tick_heartbeat symbol=%r ticks_in_last_%.0fs=%d",
                        symbol, window_seconds, counter.count,
                    )
                    counter.count = 0
                cycles = 0

    def _drain(self, symbol: str, agg: BarAggregator) -> None:
        for bar in agg.drain():
            self._store.append(symbol, bar)
            try:
                self._notify(symbol, bar)
            except Exception:
                logger.exception("notify failed for %s; bar still stored", symbol)

    @asynccontextmanager
    async def subscribe(self):
        """One queue per live GET /events connection (expected: one, from
        monitor-app's shared consumer). Registered on enter, discarded on
        exit/exception -- a dropped connection can never leak forever."""
        q: asyncio.Queue = asyncio.Queue(maxsize=self._subscriber_maxsize)
        self._subscribers.add(q)
        try:
            yield q
        finally:
            self._subscribers.discard(q)

    def _notify(self, symbol: str, bar: dict) -> None:
        """Best-effort fan-out; must never raise or block the tick path
        that stores bars (same standard as the tick-diagnostic crash found
        live 2026-09-17: instrumentation must never be able to take down
        the production message-delivery path it observes). A full queue
        (slow/stalled subscriber) drops its OLDEST item, not the newest --
        a lagging consumer cares about catching up to "now", and the
        receiving Poller's ts-dedup guard tolerates the resulting gap the
        same way it already tolerates catch-up/push overlap."""
        event = {"symbol": symbol, "bar": bar}
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    pass


def create_app(*, store: BarStore, source_factory, replay: bool,
               now_fn=time.time, history_fetcher=None,
               flush_interval: float = FLUSH_INTERVAL_SECONDS,
               heartbeat_flush_cycles: int = HEARTBEAT_FLUSH_CYCLES,
               subscriber_maxsize: int = SUBSCRIBER_QUEUE_MAXSIZE) -> FastAPI:
    connector = Connector(store=store, source_factory=source_factory,
                          replay=replay, now_fn=now_fn,
                          history_fetcher=history_fetcher,
                          flush_interval=flush_interval,
                          heartbeat_flush_cycles=heartbeat_flush_cycles,
                          subscriber_maxsize=subscriber_maxsize)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await connector.shutdown()

    app = FastAPI(title="schwab-connector", lifespan=lifespan)
    app.state.connector = connector

    @app.post("/watch")
    async def watch(req: WatchRequest):
        connector.watch(req.symbol)
        return {"watching": connector.watching}

    @app.post("/unwatch")
    async def unwatch(req: WatchRequest):
        await connector.unwatch(req.symbol)
        return {"watching": connector.watching}

    @app.get("/bars/{symbol}")
    async def get_bars(symbol: str, since_ts: float = 0.0):
        return connector.bars(symbol, since_ts)

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "watching": connector.watching,
            "connected": connector.connected,
        }

    @app.get("/events")
    async def events(request: Request):
        """Leg 1 of the poll -> push replacement (specs.md): one shared
        SSE connection streams every bar close, for every watched symbol,
        the instant it's stored -- monitor-app no longer needs to poll
        GET /bars/{symbol} on a timer to find out."""
        async def event_stream():
            async with connector.subscribe() as q:
                # Flushed immediately on connect -- confirms the connection
                # is live without waiting on the first real bar or the
                # keep-alive interval, so a fresh consumer's reconnect loop
                # never mistakes "no bars yet" for "not connected."
                yield ": connected\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        item = await asyncio.wait_for(q.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    yield f"event: bar\ndata: {json.dumps(item, separators=(',', ':'))}\n\n"
        return StreamingResponse(event_stream(), media_type="text/event-stream")

    return app
