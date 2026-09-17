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
import contextlib
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
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
    def __init__(self, *, store: BarStore, source_factory, replay: bool,
                 now_fn=time.time, flush_interval: float = FLUSH_INTERVAL_SECONDS,
                 history_fetcher=None, heartbeat_flush_cycles: int = HEARTBEAT_FLUSH_CYCLES):
        self._store = store
        self._source_factory = source_factory
        self._replay = replay
        self._now_fn = now_fn
        self._flush_interval = flush_interval
        self._history_fetcher = history_fetcher
        self._heartbeat_flush_cycles = heartbeat_flush_cycles
        self._sources: dict[str, object] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    @property
    def watching(self) -> list[str]:
        return sorted(self._sources)

    @property
    def connected(self) -> bool:
        return any(getattr(s, "connected", False) for s in self._sources.values())

    def watch(self, symbol: str) -> None:
        key = symbol.upper()
        if key in self._tasks:
            return
        self._tasks[key] = asyncio.create_task(self._consume(key))

    async def unwatch(self, symbol: str) -> bool:
        """Stop live-streaming `symbol`: cancels its consume task and drops
        it from `watching`. Idempotent -- unwatching a symbol that isn't
        currently watched is a no-op returning False. Does NOT touch its
        stored bars (BarStore) -- history stays on disk, only the live
        subscription stops, same as any other restart-safe data in this
        service. Awaits the cancelled task's actual teardown (not just
        scheduling the cancellation) so a symbol removed from `watching`
        here is fully stopped, not still mid-shutdown -- important because
        the caller (monitor-app, switching which symbol it displays) may
        re-watch a different symbol immediately after."""
        key = symbol.upper()
        task = self._tasks.pop(key, None)
        self._sources.pop(key, None)
        if task is None:
            return False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return True

    def bars(self, symbol: str, since_ts: float) -> list[dict]:
        return self._store.since(symbol, since_ts)

    async def shutdown(self) -> None:
        for t in self._tasks.values():
            t.cancel()

    # -- internals -------------------------------------------------------

    async def _consume(self, symbol: str) -> None:
        try:
            source = self._source_factory()
        except Exception:
            # Most likely a missing/expired Schwab token. The service still
            # runs: the symbol is recorded as watched, /health reports
            # connected=false, and no bars flow until a token is present.
            self._sources[symbol] = _DisconnectedSource()
            return
        self._sources[symbol] = source
        await self._maybe_backfill(symbol)
        agg = BarAggregator()
        last_tick_ts = 0.0
        tick_counter = _TickCounter()

        flusher = None
        if not self._replay:
            flusher = asyncio.create_task(self._flush_loop(symbol, agg, tick_counter))
        try:
            async for tick in source.ticks(symbol):
                tick_counter.count += 1
                agg.feed(tick)
                last_tick_ts = tick["ts"]
                self._drain(symbol, agg)
            # Stream ended (replay fixture exhausted): close the last bucket.
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

    async def _flush_loop(self, symbol: str, agg: BarAggregator,
                          tick_counter: "_TickCounter") -> None:
        cycles = 0
        while True:
            await asyncio.sleep(self._flush_interval)
            agg.flush(self._now_fn())
            self._drain(symbol, agg)
            cycles += 1
            if cycles >= self._heartbeat_flush_cycles:
                window_seconds = cycles * self._flush_interval
                tick_logger.info(
                    "event=tick_heartbeat symbol=%r ticks_in_last_%.0fs=%d",
                    symbol, window_seconds, tick_counter.count,
                )
                tick_counter.count = 0
                cycles = 0

    def _drain(self, symbol: str, agg: BarAggregator) -> None:
        for bar in agg.drain():
            self._store.append(symbol, bar)


def create_app(*, store: BarStore, source_factory, replay: bool,
               now_fn=time.time, history_fetcher=None,
               flush_interval: float = FLUSH_INTERVAL_SECONDS,
               heartbeat_flush_cycles: int = HEARTBEAT_FLUSH_CYCLES) -> FastAPI:
    connector = Connector(store=store, source_factory=source_factory,
                          replay=replay, now_fn=now_fn,
                          history_fetcher=history_fetcher,
                          flush_interval=flush_interval,
                          heartbeat_flush_cycles=heartbeat_flush_cycles)

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

    return app
