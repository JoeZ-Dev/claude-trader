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
from dataclasses import asdict, dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

_CORE = os.environ.get("CORE_PATH") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core"
)
if _CORE not in sys.path:
    sys.path.insert(0, _CORE)

from indicators import (  # noqa: E402
    ema_time_aware, macd_time_aware, relative_volume_time_aware, session_vwap,
    SessionVwapState, EmaTimeAwareState, MacdTimeAwareState, RelativeVolumeState,
)
from levels import detect_levels, evaluate_hold_time_aware, EvaluateHoldTimeAwareState  # noqa: E402
from setup_types import evaluate_setups, evaluate_breakdown_setups  # noqa: E402

_NY = ZoneInfo("America/New_York")
REQUIRED_HOLD_SECONDS = 30.0
RELVOL_LOOKBACK_SECONDS = 200.0


@dataclass
class IncrementalState:
    """Persistent, per-symbol cross-call state for `build_state`'s five
    incrementalized functions (specs.md section 28/29, build_state
    incremental architecture). Lives on `_SymbolSlot` in monitor-app/
    app.py -- add_symbol() always constructs a BRAND NEW `_SymbolSlot`
    (and so a brand new, genuinely empty `IncrementalState`) for a
    newly-watched symbol, and remove_symbol() drops the slot entirely,
    so a later re-add starts fresh too, never inheriting anything from
    the previous watch period. A process restart also constructs a
    fresh instance the same way -- `build_state` reconstructs correctly
    from the existing bar history the first time it's called with this
    empty state (each sub-state's own `from_bars`/`from_series`
    rebuild), then stays incremental from that point forward.

    `processed_count` is the single source of truth for "how many of the
    `bars` list passed to `build_state` have already been folded into
    the five sub-states below" -- all five see the SAME growing `bars`
    list, so one counter (not one per function) is enough to compute
    `new_bars = bars[processed_count:]` for all of them.

    `hold_above`/`hold_below` are additionally keyed to a specific
    level price (`EvaluateHoldTimeAwareState.level_price`/`.direction`):
    `detect_levels` reruns in full every call (explicitly out of this
    redesign's scope) and can pick a DIFFERENT level over time, which
    invalidates whatever streak state was being tracked for the OLD
    level -- `build_state` detects that and rebuilds via `from_bars`
    when it happens, same "caller owns the boundary decision" split
    every other sub-state uses for its own reset condition."""
    processed_count: int = 0
    vwap: SessionVwapState | None = None
    vwap_session_date: date | None = None
    # Today's real opening price (specs.md section 37, pattern-flags
    # feature) -- captured ONCE, alongside vwap's own one-time rebuild
    # at a real session rollover (same session_bars_for_vwap(bars) call,
    # not a second filter), then read cheaply on every other call. Reset
    # naturally whenever vwap itself resets, since both track the SAME
    # session boundary.
    session_day_open: float | None = None
    ema9: EmaTimeAwareState | None = None
    ema20: EmaTimeAwareState | None = None
    macd: MacdTimeAwareState | None = None
    hold_above: EvaluateHoldTimeAwareState | None = None
    hold_below: EvaluateHoldTimeAwareState | None = None
    relvol: RelativeVolumeState | None = None


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


def _resolve_hold(bars, new_bars, level, direction: str, watch_added_ts: float | None,
                  incremental: "IncrementalState | None", attr: str):
    """Returns the hold-confirmation result for `level`/`direction` --
    either via a full recompute (the OLD path, when `incremental` is
    None) or via `IncrementalState`'s own `EvaluateHoldTimeAwareState`
    for `attr` ("hold_above"/"hold_below"), stepping only `new_bars`
    when the tracked state is still for the SAME level/direction/
    watch_added_ts, or doing a one-time `from_bars` rebuild (and storing
    the fresh state back onto `incremental`) when it isn't -- the level
    just changed, this is the symbol's first-ever call, or there's
    nothing new to process (in which case the cached `last_result` is
    returned untouched, matching what a full recompute over the SAME
    unchanged bars list would still return)."""
    if level is None:
        if incremental is not None:
            setattr(incremental, attr, None)
        return None
    if incremental is None:
        return evaluate_hold_time_aware(bars, level.price, direction=direction,
                                        required_seconds=REQUIRED_HOLD_SECONDS,
                                        watch_added_ts=watch_added_ts)
    state = getattr(incremental, attr)
    if (state is None or state.level_price != level.price
            or state.direction != direction or state.watch_added_ts != watch_added_ts):
        state = EvaluateHoldTimeAwareState.from_bars(
            bars, level.price, direction=direction,
            required_seconds=REQUIRED_HOLD_SECONDS, watch_added_ts=watch_added_ts,
        )
        setattr(incremental, attr, state)
        return state.last_result
    for b in new_bars:
        state.update(b)
    return state.last_result


def _level_block(level, hold) -> dict | None:
    if level is None:
        return None
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
            "direction": hold.direction,
            "required_seconds": REQUIRED_HOLD_SECONDS,
            "elapsed_seconds": hold.elapsed_seconds,
            "confirmed": hold.confirmed,
            "failed_attempts": hold.failed_attempts,
            "confirmed_at_ts": hold.confirmed_at_ts,
        },
    }


def build_state(bars: list[dict], symbol: str | None = None,
                watch_added_ts: float | None = None,
                incremental: "IncrementalState | None" = None) -> dict:
    """`watch_added_ts` (phase 3.6 stage 3 part 2, specs.md section 19):
    this symbol's own watch-start time (epoch seconds), threaded through
    to every hold-confirmation call so a hold can't show "confirmed" from
    purely pre-watch backfilled history the instant a symbol is added --
    see `evaluate_hold_time_aware`'s docstring for the full real-data
    finding this guards against. `None` (the default) disables the
    guard, for callers that don't track a watch time.

    `incremental` (specs.md section 28/29, build_state incremental
    architecture): `None` (the default) is the ORIGINAL, unchanged
    full-recompute path -- every one of the five now-incrementalized
    computations (session VWAP, ema9/ema20, MACD, relative volume, both
    levels' hold-confirmation) is rebuilt from the ENTIRE `bars` list on
    every call, exactly as it always has been. Given an `IncrementalState`
    (monitor-app/app.py's `_SymbolSlot.incremental`, reused across every
    call for a given symbol), only the bars ADDED since the last call
    (`bars[incremental.processed_count:]`) are folded into each
    sub-state, and the sub-state's own already-proven-equivalent
    `update()`/`from_bars()`/`from_series()` methods do the rest --
    `detect_levels`/swing-point detection (explicitly out of THIS
    redesign's scope) still recomputes from the full `bars` list every
    call, unaffected either way."""
    if not bars:
        return {"status": "warming_up", "symbol": symbol, "bar_count": 0}

    last_price = bars[-1]["close"]
    closes = [b["close"] for b in bars]
    timestamps = [b["ts"] for b in bars]
    new_bars = bars[incremental.processed_count:] if incremental is not None else bars

    if incremental is None:
        session = session_bars_for_vwap(bars)
        vwap = session_vwap(session)[-1] if session else None
        day_open = session[0]["open"] if session else None
        # today's cumulative session volume (specs.md section 12's
        # session-level volume gate) -- the SAME session slice VWAP
        # already uses above, not a separately-invented one.
        cumulative_volume = sum(b["volume"] for b in session)
    else:
        latest_date = _ny_date(bars[-1]["ts"])
        if incremental.vwap is None or incremental.vwap_session_date != latest_date:
            # A new session (or the first-ever call): session_bars_for_
            # vwap's own O(n) filter to "today's" bars is unavoidable
            # here (out of scope -- this is a ONE-TIME cost per real
            # session rollover, not per bar), then SessionVwapState folds
            # in exactly that slice, same as the full-recompute path
            # would compute over it. day_open captured from this SAME
            # slice/call, not a second filter.
            today_session = session_bars_for_vwap(bars)
            incremental.vwap = SessionVwapState.from_bars(today_session)
            incremental.vwap_session_date = latest_date
            incremental.session_day_open = today_session[0]["open"] if today_session else None
        else:
            for b in new_bars:
                incremental.vwap.update(b)
        vwap = incremental.vwap.last_value
        day_open = incremental.session_day_open
        # SessionVwapState.cum_vol IS exactly sum(b["volume"] for b in
        # session) -- both accumulate the SAME today-only bars' volume,
        # so reusing it here avoids a second full-history date filter.
        cumulative_volume = incremental.vwap.cum_vol

    # ema/macd/relative_volume/hold-confirmation now all see the FULL
    # backfilled+live series directly (specs.md section 19) -- their
    # time-aware versions correctly weight whatever cadence each bar
    # actually has, replacing the old live_cadence_tail split entirely.
    if incremental is None:
        ema9_val = ema_time_aware(closes, timestamps, 9)[-1]
        ema20_val = ema_time_aware(closes, timestamps, 20)[-1]
        macd_result = macd_time_aware(closes, timestamps)
        macd_val = macd_result["macd"][-1]
        signal_val = macd_result["signal"][-1]
        histogram_val = macd_result["histogram"][-1]
        relvol = relative_volume_time_aware(bars, lookback_seconds=RELVOL_LOOKBACK_SECONDS)[-1]
    else:
        if incremental.ema9 is None:
            incremental.ema9 = EmaTimeAwareState.from_series(closes, timestamps, 9)
        else:
            for i in range(incremental.processed_count, len(bars)):
                incremental.ema9.update(closes[i], timestamps[i])
        if incremental.ema20 is None:
            incremental.ema20 = EmaTimeAwareState.from_series(closes, timestamps, 20)
        else:
            for i in range(incremental.processed_count, len(bars)):
                incremental.ema20.update(closes[i], timestamps[i])
        if incremental.macd is None:
            incremental.macd = MacdTimeAwareState.from_series(closes, timestamps)
        else:
            for i in range(incremental.processed_count, len(bars)):
                incremental.macd.update(closes[i], timestamps[i])
        if incremental.relvol is None:
            incremental.relvol = RelativeVolumeState.from_bars(bars, lookback_seconds=RELVOL_LOOKBACK_SECONDS)
        else:
            for b in new_bars:
                incremental.relvol.update(b)

        ema9_val = incremental.ema9.value
        ema20_val = incremental.ema20.value
        macd_val = incremental.macd.fast.value - incremental.macd.slow.value
        signal_val = incremental.macd.signal.value
        histogram_val = macd_val - signal_val
        relvol = incremental.relvol.last_value

    picked = select_levels(detect_levels(bars), last_price)
    hold_above = _resolve_hold(bars, new_bars, picked["resistance"], "above",
                               watch_added_ts, incremental, "hold_above")
    hold_below = _resolve_hold(bars, new_bars, picked["support"], "below",
                               watch_added_ts, incremental, "hold_below")

    if incremental is not None:
        incremental.processed_count = len(bars)

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
            # Today's real opening price (specs.md section 37, pattern-
            # flags feature) -- the first bar of the SAME session slice
            # VWAP already uses, not a separately-invented "day start"
            # concept; a cheap addition, not a new capture pipeline.
            "day_open": day_open,
            "vwap": round(vwap, 4) if vwap is not None else None,
            "ema9": round(ema9_val, 4),
            "ema20": round(ema20_val, 4),
            "macd": {
                "macd": round(macd_val, 6),
                "signal": round(signal_val, 6),
                "histogram": round(histogram_val, 6),
            },
            "relative_volume": round(relvol, 4),
            "cumulative_volume": round(cumulative_volume, 4),
        },
        "levels": {
            "resistance": _level_block(picked["resistance"], hold_above),
            "support": _level_block(picked["support"], hold_below),
        },
        "setups": setups,
        "breakdown_setups": breakdown_setups,
    }
