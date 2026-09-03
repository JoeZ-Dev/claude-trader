"""
schwab-connector FastAPI app.

The only container that holds the Schwab OAuth token. Owns the stream
subscription and 10s-bar aggregation, and exposes the internal API defined
in specs.md section 5 (internal only -- docker-compose does not publish a
port for this service):

  POST /watch {"symbol": "..."}
  GET  /bars/{symbol}?since_ts={unix_seconds}  -> array of bar objects
                                                  (specs.md section 4 shape)
  GET  /health  -> {"status": "ok", "watching": [...], "connected": bool}

create_app() takes its dependencies as arguments so tests can inject a
ReplayStreamSource and a temp BarStore. main.py wires the real ones.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

from aggregator import BUCKET_SECONDS, BarAggregator
from store import BarStore

FLUSH_INTERVAL_SECONDS = 2.0


class WatchRequest(BaseModel):
    symbol: str


class _DisconnectedSource:
    """Placeholder for a symbol that was asked for but has no working stream
    (e.g. no Schwab token yet)."""
    connected = False


class Connector:
    def __init__(self, *, store: BarStore, source_factory, replay: bool,
                 now_fn=time.time, flush_interval: float = FLUSH_INTERVAL_SECONDS):
        self._store = store
        self._source_factory = source_factory
        self._replay = replay
        self._now_fn = now_fn
        self._flush_interval = flush_interval
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
        agg = BarAggregator()
        last_tick_ts = 0.0

        flusher = None
        if not self._replay:
            flusher = asyncio.create_task(self._flush_loop(symbol, agg))
        try:
            async for tick in source.ticks(symbol):
                agg.feed(tick)
                last_tick_ts = tick["ts"]
                self._drain(symbol, agg)
            # Stream ended (replay fixture exhausted): close the last bucket.
            agg.flush(last_tick_ts + BUCKET_SECONDS)
            self._drain(symbol, agg)
        finally:
            if flusher is not None:
                flusher.cancel()

    async def _flush_loop(self, symbol: str, agg: BarAggregator) -> None:
        while True:
            await asyncio.sleep(self._flush_interval)
            agg.flush(self._now_fn())
            self._drain(symbol, agg)

    def _drain(self, symbol: str, agg: BarAggregator) -> None:
        for bar in agg.drain():
            self._store.append(symbol, bar)


def create_app(*, store: BarStore, source_factory, replay: bool,
               now_fn=time.time) -> FastAPI:
    connector = Connector(store=store, source_factory=source_factory,
                          replay=replay, now_fn=now_fn)

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
