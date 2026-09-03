"""
Stream sources for schwab-connector.

The container needs live ticks from Schwab in production, but the definition
of done also has to be demonstrable outside market hours and without a live
token. So tick production sits behind a small seam with two implementations:

- SchwabStreamSource  -- real: wraps schwab-py's StreamClient.
- ReplayStreamSource  -- a recorded fixture (ticks or bars) replayed back
                         through the same interface. Used by tests and by
                         `docker-compose up` in STREAM_SOURCE=replay mode.

Both yield the internal tick shape (never leaves this container):

    {"ts": float, "price": float, "size": float}

`message_to_ticks` is the pure Schwab-payload -> ticks mapping, kept
separate so it can be unit-tested against captured payloads without a live
stream (per AGENT_PROTOCOL.md: no test touches a live external service).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import AsyncIterator

# Schwab LEVELONE_EQUITIES: field 3 = last price, 9 = last size, 8 = total
# volume. schwab-py emits either the numeric keys or these names depending on
# how the StreamClient is configured, so accept both.
_PRICE_KEYS = ("LAST_PRICE", "3")
_SIZE_KEYS = ("LAST_SIZE", "9")


def _first(entry: dict, keys) -> float | None:
    for k in keys:
        if k in entry and entry[k] is not None:
            return float(entry[k])
    return None


def message_to_ticks(msg: dict) -> list[tuple[str, dict]]:
    """Map one LEVELONE_EQUITIES message to (symbol, tick) pairs.

    A content entry with no last price is skipped (it is a quote-only update
    such as a bid/ask change). A last price with no last size becomes a
    zero-volume tick: it still moves the bar's high/low/close without
    inflating volume.
    """
    ts = float(msg.get("timestamp", 0)) / 1000.0
    out: list[tuple[str, dict]] = []
    for entry in msg.get("content", []):
        price = _first(entry, _PRICE_KEYS)
        if price is None:
            continue
        size = _first(entry, _SIZE_KEYS)
        out.append((
            entry.get("key", ""),
            {"ts": ts, "price": price, "size": 0.0 if size is None else size},
        ))
    return out


class ReplayStreamSource:
    """Replays a JSONL fixture of ticks or bars.

    Each line is either an internal tick ({"ts","price","size"}) or a bar
    (has "open"/"close"). A bar is exploded into four ticks at +0/+1/+2/+3s
    within its 10s bucket, in open-high-low-close order, with the whole bar
    volume placed on the first tick and 0 on the rest -- so feeding the
    output back through BarAggregator reconstructs the identical bar.
    """

    def __init__(self, path, pace: bool = False, speed: float = 60.0) -> None:
        self._path = Path(path)
        self._pace = pace
        self._speed = speed
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def ticks(self, symbol: str) -> AsyncIterator[dict]:
        self._connected = True
        prev_ts: float | None = None
        for row in self._read_rows():
            for tick in self._row_to_ticks(row):
                if self._pace and prev_ts is not None:
                    delay = (tick["ts"] - prev_ts) / max(self._speed, 1e-9)
                    if delay > 0:
                        await asyncio.sleep(delay)
                prev_ts = tick["ts"]
                yield tick

    def _read_rows(self):
        text = self._path.read_text()
        for line in text.splitlines():
            line = line.strip()
            if line:
                yield json.loads(line)

    @staticmethod
    def _row_to_ticks(row: dict) -> list[dict]:
        if "price" in row:  # already a tick
            return [{"ts": float(row["ts"]), "price": float(row["price"]),
                     "size": float(row.get("size", 0.0))}]
        base = int(row["ts"])
        vol = float(row.get("volume", 0.0))
        seq = [("open", vol), ("high", 0.0), ("low", 0.0), ("close", 0.0)]
        return [{"ts": float(base + i), "price": float(row[field]), "size": sz}
                for i, (field, sz) in enumerate(seq)]


class SchwabStreamSource:
    """Wraps schwab-py's StreamClient. Not exercised by the test suite (it
    needs a live token and network); the payload mapping it relies on is
    covered via message_to_ticks."""

    def __init__(self, client) -> None:
        self._client = client
        self._stream = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._connected = False
        self._symbol: str | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    async def ticks(self, symbol: str) -> AsyncIterator[dict]:
        from schwab.streaming import StreamClient

        self._symbol = symbol.upper()
        self._stream = StreamClient(self._client)
        await self._stream.login()
        self._connected = True
        self._stream.add_level_one_equity_handler(self._on_message)
        await self._stream.level_one_equity_subs([self._symbol])

        async def _pump():
            while True:
                await self._stream.handle_message()

        pump_task = asyncio.create_task(_pump())
        try:
            while True:
                yield await self._queue.get()
        finally:
            pump_task.cancel()
            self._connected = False

    def _on_message(self, msg: dict) -> None:
        for sym, tick in message_to_ticks(msg):
            if sym.upper() == self._symbol:
                self._queue.put_nowait(tick)
