"""
Tick -> 10-second-bar aggregation for schwab-connector.

Pure logic: no I/O, no network, no framework. Fed a stream of internal ticks
and produces finalized bars matching the contract in specs.md section 4 (the
shape every other component consumes):

    {
      "ts": int,          # unix seconds (bucket start, multiple of 10)
      "open": float,
      "high": float,
      "low": float,
      "close": float,
      "volume": float,
      "is_extended": bool  # true for premarket/after-hours bars
    }

Internal tick shape (produced by whichever StreamSource is active, never
leaves this container):

    {"ts": float, "price": float, "size": float}

Design decisions locked for phase 1:

- Empty 10s windows are FORWARD-FILLED: a window with no ticks emits a flat
  bar (open == high == low == close == the previous bar's close) with
  volume 0.0. This keeps the series on a contiguous 10s grid so EMA/MACD
  and hold-confirmation see evenly spaced closes, and so the web view lines
  up with how charting platforms render quiet periods. Forward-fill only
  begins once a first real bar has been finalized.
- Out-of-order ticks (a tick whose bucket is older than the currently open
  bucket) are dropped. The Schwab stream is effectively monotonic; a late
  straggler is not worth reordering machinery in phase 1.
- Regular trading hours (RTH) are Mon-Fri 09:30:00-16:00:00 America/New_York.
  Anything outside that window is "extended". No market-holiday calendar is
  consulted in phase 1: a bar on a holiday during RTH clock hours will be
  labelled is_extended=False. Documented limitation, not an oversight.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

BUCKET_SECONDS = 10

_NY = ZoneInfo("America/New_York")
_RTH_OPEN_MINUTES = 9 * 60 + 30   # 09:30
_RTH_CLOSE_MINUTES = 16 * 60      # 16:00


def bucket_start(ts: float) -> int:
    """Floor a timestamp to the start of its 10-second bucket."""
    return int(ts // BUCKET_SECONDS) * BUCKET_SECONDS


def is_extended_hours(ts: float) -> bool:
    """True if the given unix timestamp falls outside RTH (Mon-Fri
    09:30:00-16:00:00 America/New_York). See module docstring for the
    holiday-calendar caveat."""
    dt = datetime.fromtimestamp(ts, _NY)
    if dt.weekday() >= 5:  # Saturday=5, Sunday=6
        return True
    minutes = dt.hour * 60 + dt.minute
    return not (_RTH_OPEN_MINUTES <= minutes < _RTH_CLOSE_MINUTES)


class BarAggregator:
    """Accumulates ticks into 10s bars.

    Usage: feed() ticks as they arrive; call flush(now_ts) on a timer to
    close out a bucket that has gone quiet; call drain() to collect any
    finalized bars (returns them once, then forgets them).
    """

    def __init__(self) -> None:
        self._cur_start: int | None = None
        self._open: float | None = None
        self._high: float | None = None
        self._low: float | None = None
        self._close: float | None = None
        self._volume: float = 0.0

        self._last_filled_start: int | None = None
        self._last_close: float | None = None

        self._ready: list[dict] = []

    def feed(self, tick: dict) -> None:
        b = bucket_start(tick["ts"])

        if self._cur_start is None:
            self._open_bucket(b, tick)
            return

        if b == self._cur_start:
            self._apply(tick)
        elif b > self._cur_start:
            self._finalize_current()
            self._fill_gap_until(b)
            self._open_bucket(b, tick)
        # b < self._cur_start: out-of-order straggler, dropped on purpose.

    def flush(self, now_ts: float) -> None:
        """Finalize the open bucket and forward-fill flat bars up to (but not
        including) the bucket containing now_ts, when that bucket is newer
        than the one currently open."""
        if self._cur_start is None:
            return
        target = bucket_start(now_ts)
        if target > self._cur_start:
            self._finalize_current()
            self._fill_gap_until(target)

    def drain(self) -> list[dict]:
        """Return finalized bars accumulated since the last call, then clear."""
        out, self._ready = self._ready, []
        return out

    # -- internals -----------------------------------------------------------

    def _open_bucket(self, start: int, tick: dict) -> None:
        self._cur_start = start
        self._open = self._high = self._low = self._close = tick["price"]
        self._volume = float(tick["size"])

    def _apply(self, tick: dict) -> None:
        p = tick["price"]
        self._high = max(self._high, p)
        self._low = min(self._low, p)
        self._close = p
        self._volume += float(tick["size"])

    def _finalize_current(self) -> None:
        bar = {
            "ts": self._cur_start,
            "open": self._open,
            "high": self._high,
            "low": self._low,
            "close": self._close,
            "volume": self._volume,
            "is_extended": is_extended_hours(self._cur_start),
        }
        self._ready.append(bar)
        self._last_filled_start = self._cur_start
        self._last_close = self._close
        self._cur_start = None
        self._open = self._high = self._low = self._close = None
        self._volume = 0.0

    def _fill_gap_until(self, next_start: int) -> None:
        """Emit flat zero-volume bars for every empty bucket strictly between
        the last finalized bucket and next_start."""
        if self._last_filled_start is None or self._last_close is None:
            return
        g = self._last_filled_start + BUCKET_SECONDS
        while g < next_start:
            self._ready.append({
                "ts": g,
                "open": self._last_close,
                "high": self._last_close,
                "low": self._last_close,
                "close": self._last_close,
                "volume": 0.0,
                "is_extended": is_extended_hours(g),
            })
            self._last_filled_start = g
            g += BUCKET_SECONDS
