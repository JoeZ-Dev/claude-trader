"""
monitor-app state builder -- pure, no I/O.

Takes the full list of bars held so far and runs a COMPLETE recompute
through momentum_monitor/core on every call (specs.md section 3: the core
is cheap and full recompute keeps the app trivially correct -- no
incremental-update bugs). Produces the dict served verbatim at
GET /api/state.

Three policy choices for phase 1, from the build session:

- Session VWAP is anchored at the first bar of the latest bar's
  America/New_York calendar date (premarket bars included in the
  accumulation). Premarket/after-hours bars are still stored and shown;
  they are simply part of the day's VWAP.
- Hold-confirmation is evaluated for TWO levels only: the strongest
  resistance priced ABOVE the last price (direction "above") and the
  strongest support priced BELOW it (direction "below"). Entry-side
  evaluation only -- there is no stop-loss evaluation anywhere in this
  tool. required_bars = 3 (the core default).
- Bar-count-windowed indicators (ema/macd/relative_volume/hold-
  confirmation) only ever see the LIVE, uniform-10s-cadence tail of the
  bar list, never the coarser/irregular backfilled bars schwab-connector
  prepends ahead of it (see price_history.py there, and specs.md section
  3, "Backfill vs. live bar width"). session_vwap and detect_levels are
  not window-based this way and keep seeing the full backfilled+live
  series on purpose -- see live_cadence_tail() below for why and how the
  split is found.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

_CORE = os.environ.get("CORE_PATH") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core"
)
if _CORE not in sys.path:
    sys.path.insert(0, _CORE)

from indicators import ema, macd, relative_volume, session_vwap  # noqa: E402
from levels import detect_levels, evaluate_hold  # noqa: E402

_NY = ZoneInfo("America/New_York")
REQUIRED_HOLD_BARS = 3
RELVOL_LOOKBACK = 20

# schwab-connector's live aggregator (aggregator.py, BUCKET_SECONDS=10)
# forward-fills exactly one bar per bucket once streaming has started, so
# no two consecutive live bars are ever more than 10s apart. Backfilled
# bars (price_history.py) are Schwab price-history candles no finer than
# 1 minute, and Schwab omits zero-volume minutes entirely, so consecutive
# backfilled bars can be 60s to 900+s apart. 15s sits safely between the
# two: comfortably above live's 10s cadence (with slack for jitter),
# comfortably below backfill's 60s floor. This constant intentionally
# duplicates schwab-connector's BUCKET_SECONDS rather than importing it --
# the two containers don't share code across that boundary
# (AGENT_PROTOCOL.md's directory-boundary rule) or even run in the same
# process; the boundary between them is detected from the DATA (the
# spacing between bars monitor-app already receives), not from a shared
# constant.
LIVE_BAR_MAX_GAP_SECONDS = 15.0


def _ny_date(ts: float):
    return datetime.fromtimestamp(ts, _NY).date()


def session_bars_for_vwap(bars: list[dict]) -> list[dict]:
    """Bars that share the most recent bar's America/New_York calendar date."""
    if not bars:
        return []
    latest = _ny_date(bars[-1]["ts"])
    return [b for b in bars if _ny_date(b["ts"]) == latest]


def live_cadence_tail(bars: list[dict],
                      max_gap: float = LIVE_BAR_MAX_GAP_SECONDS) -> list[dict]:
    """The contiguous suffix of `bars` produced at live-stream cadence,
    isolated from any coarser/irregular backfilled history ahead of it.

    Walks backward from the most recent bar; a gap larger than `max_gap`
    marks the boundary where backfilled history ends and live streaming
    begins. When there's no backfill at all (replay mode, or a fresh watch
    whose backfill hasn't landed yet), every bar already has live cadence
    and the whole list comes back unchanged -- this is what keeps existing
    all-live-bar behavior (and its tests) unaffected by this function's
    existence."""
    if not bars:
        return []
    i = len(bars) - 1
    while i > 0 and bars[i]["ts"] - bars[i - 1]["ts"] <= max_gap:
        i -= 1
    return bars[i:]


def select_levels(levels, current_price: float) -> dict:
    """Strongest resistance above price, strongest support below price."""
    above = [l for l in levels
             if l.kind == "resistance" and l.price > current_price]
    below = [l for l in levels
             if l.kind == "support" and l.price < current_price]
    return {
        "resistance": max(above, key=lambda l: l.strength_score) if above else None,
        "support": max(below, key=lambda l: l.strength_score) if below else None,
    }


def _level_block(bars, level, direction: str) -> dict | None:
    if level is None:
        return None
    hold = evaluate_hold(bars, level.price, direction=direction,
                         required_bars=REQUIRED_HOLD_BARS)
    return {
        "price": round(level.price, 4),
        "kind": level.kind,
        "strength_score": round(level.strength_score, 4),
        # Components stay separate, never collapsed into the score alone
        # (specs.md section 3).
        "components": {
            "touch_count": level.touch_count,
            "total_touch_volume": level.total_touch_volume,
            "round_number_bonus": round(level.round_number_bonus, 4),
        },
        "hold": {
            "direction": direction,
            "required_bars": REQUIRED_HOLD_BARS,
            "consecutive_bars": hold.consecutive_bars,
            "confirmed": hold.confirmed,
            "failed_attempts": hold.failed_attempts,
        },
    }


def build_state(bars: list[dict], symbol: str | None = None) -> dict:
    if not bars:
        return {"status": "warming_up", "symbol": symbol, "bar_count": 0}

    last_price = bars[-1]["close"]

    session = session_bars_for_vwap(bars)
    vwap = session_vwap(session)[-1] if session else None

    # ema/macd/relative_volume/hold-confirmation are bar-count-windowed and
    # implicitly assume uniform bar width -- see the module docstring and
    # live_cadence_tail() above. detect_levels (level identification) and
    # session_vwap, above, are not window-based this way and deliberately
    # keep seeing the full backfilled+live series.
    live_bars = live_cadence_tail(bars)
    live_closes = [b["close"] for b in live_bars]

    macd_result = macd(live_closes)
    relvol = relative_volume(live_bars, lookback=RELVOL_LOOKBACK)[-1]

    picked = select_levels(detect_levels(bars), last_price)

    return {
        "status": "ok",
        "symbol": symbol,
        "bar_count": len(bars),
        "last_price": round(last_price, 4),
        "last_bar_ts": bars[-1]["ts"],
        "last_bar_is_extended": bars[-1]["is_extended"],
        "session": {
            "vwap": round(vwap, 4) if vwap is not None else None,
            "ema9": round(ema(live_closes, 9)[-1], 4),
            "ema20": round(ema(live_closes, 20)[-1], 4),
            "macd": {
                "macd": round(macd_result["macd"][-1], 6),
                "signal": round(macd_result["signal"][-1], 6),
                "histogram": round(macd_result["histogram"][-1], 6),
            },
            "relative_volume": round(relvol, 4),
        },
        "levels": {
            "resistance": _level_block(live_bars, picked["resistance"], "above"),
            "support": _level_block(live_bars, picked["support"], "below"),
        },
    }
