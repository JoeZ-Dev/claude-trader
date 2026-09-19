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
  tool. required_seconds = 30.0 (the core default, 3 bars' worth at live
  10s cadence).
- Migrated (phase 3.6 stage 3 part 2, specs.md section 19) OFF the old
  `live_cadence_tail` split: ema/macd/relative_volume/hold-confirmation
  now all see the FULL backfilled+live series directly, via their
  time-aware versions (sections 15-17), which correctly weight whatever
  cadence each bar actually has instead of needing a pre-filtered
  uniform-cadence subset. `live_cadence_tail` itself is retired --
  nothing in this module depends on it anymore.

Phase 3.5 addition: `setups` runs core/setup_types.py's evaluate_setups()
alongside the existing resistance/support block, now sharing the SAME
single `bars` list (no more separate `live_bars` split -- see above).
"""
from __future__ import annotations

import os
import sys
from dataclasses import asdict
from datetime import datetime
from zoneinfo import ZoneInfo

_CORE = os.environ.get("CORE_PATH") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core"
)
if _CORE not in sys.path:
    sys.path.insert(0, _CORE)

from indicators import ema_time_aware, macd_time_aware, relative_volume_time_aware, session_vwap  # noqa: E402
from levels import detect_levels, evaluate_hold_time_aware  # noqa: E402
from setup_types import evaluate_setups, evaluate_breakdown_setups  # noqa: E402

_NY = ZoneInfo("America/New_York")
REQUIRED_HOLD_SECONDS = 30.0
RELVOL_LOOKBACK_SECONDS = 200.0


def _ny_date(ts: float):
    return datetime.fromtimestamp(ts, _NY).date()


def session_bars_for_vwap(bars: list[dict]) -> list[dict]:
    """Bars that share the most recent bar's America/New_York calendar date."""
    if not bars:
        return []
    latest = _ny_date(bars[-1]["ts"])
    return [b for b in bars if _ny_date(b["ts"]) == latest]


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


def _level_block(bars, level, direction: str, watch_added_ts: float | None) -> dict | None:
    if level is None:
        return None
    hold = evaluate_hold_time_aware(bars, level.price, direction=direction,
                                    required_seconds=REQUIRED_HOLD_SECONDS,
                                    watch_added_ts=watch_added_ts)
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
            "required_seconds": REQUIRED_HOLD_SECONDS,
            "elapsed_seconds": hold.elapsed_seconds,
            "confirmed": hold.confirmed,
            "failed_attempts": hold.failed_attempts,
            "confirmed_at_ts": hold.confirmed_at_ts,
        },
    }


def build_state(bars: list[dict], symbol: str | None = None,
                watch_added_ts: float | None = None) -> dict:
    """`watch_added_ts` (phase 3.6 stage 3 part 2, specs.md section 19):
    this symbol's own watch-start time (epoch seconds), threaded through
    to every hold-confirmation call so a hold can't show "confirmed" from
    purely pre-watch backfilled history the instant a symbol is added --
    see `evaluate_hold_time_aware`'s docstring for the full real-data
    finding this guards against. `None` (the default) disables the
    guard, for callers that don't track a watch time."""
    if not bars:
        return {"status": "warming_up", "symbol": symbol, "bar_count": 0}

    last_price = bars[-1]["close"]

    session = session_bars_for_vwap(bars)
    vwap = session_vwap(session)[-1] if session else None
    # today's cumulative session volume (specs.md section 12's session-
    # level volume gate) -- the SAME session slice VWAP already uses
    # above, not a separately-invented one.
    cumulative_volume = sum(b["volume"] for b in session)

    # ema/macd/relative_volume/hold-confirmation now all see the FULL
    # backfilled+live series directly (specs.md section 19) -- their
    # time-aware versions correctly weight whatever cadence each bar
    # actually has, replacing the old live_cadence_tail split entirely.
    closes = [b["close"] for b in bars]
    timestamps = [b["ts"] for b in bars]

    macd_result = macd_time_aware(closes, timestamps)
    relvol = relative_volume_time_aware(bars, lookback_seconds=RELVOL_LOOKBACK_SECONDS)[-1]

    picked = select_levels(detect_levels(bars), last_price)

    # Already sorted ascending by dollar distance to trigger (setup_types.
    # evaluate_setups' own contract) -- setups[0], if present, is
    # "closest." A type that isn't currently watchable (no level above
    # price, no real VWAP pullback in progress) is simply absent, not a
    # null placeholder entry.
    setups = [asdict(c) for c in
              evaluate_setups(bars, last_price, vwap, watch_added_ts=watch_added_ts)]

    # Breakdown-below variants (specs.md section 22) -- warning/context
    # signals only, NEVER a trade trigger. Deliberately kept in a
    # completely separate key, never merged into `setups` above: app.py's
    # _update_journal only ever reads `setups` when calling advance_journal,
    # so a breakdown candidate has no code path into entry-decision logic
    # at all (see monitor-app/journal_logic.py's _ENTRY_ELIGIBLE_SETUP_TYPES
    # for the second, defense-in-depth layer on top of this separation).
    breakdown_setups = [asdict(c) for c in
                       evaluate_breakdown_setups(bars, last_price, vwap,
                                                 watch_added_ts=watch_added_ts)]

    return {
        "status": "ok",
        "symbol": symbol,
        "bar_count": len(bars),
        "last_price": round(last_price, 4),
        "last_bar_ts": bars[-1]["ts"],
        "last_bar_is_extended": bars[-1]["is_extended"],
        "session": {
            "vwap": round(vwap, 4) if vwap is not None else None,
            "ema9": round(ema_time_aware(closes, timestamps, 9)[-1], 4),
            "ema20": round(ema_time_aware(closes, timestamps, 20)[-1], 4),
            "macd": {
                "macd": round(macd_result["macd"][-1], 6),
                "signal": round(macd_result["signal"][-1], 6),
                "histogram": round(macd_result["histogram"][-1], 6),
            },
            "relative_volume": round(relvol, 4),
            "cumulative_volume": round(cumulative_volume, 4),
        },
        "levels": {
            "resistance": _level_block(bars, picked["resistance"], "above", watch_added_ts),
            "support": _level_block(bars, picked["support"], "below", watch_added_ts),
        },
        "setups": setups,
        "breakdown_setups": breakdown_setups,
    }
