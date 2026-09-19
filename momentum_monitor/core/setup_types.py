"""
Multi-scenario setup evaluation (specs.md phase 3.5, roadmap item 3.5).

Rather than surfacing only the nearest above/below level (state.py's
existing select_levels -- phase 1's simpler design), this module
evaluates several DISTINCT candidate setup TYPES in parallel and lets the
caller compare them side by side. Direct evolution of ToS_Companion's
candidate_generator.py three-setup-type design, rebuilt on this repo's
corrected level detection (detect_levels/evaluate_hold) instead of
ToS_Companion's buggy nearest-price picking.

Scope for this pass (deliberate, not an oversight -- see specs.md):
bullish/breakout-ABOVE direction only. A symmetric breakdown-below
version of each type is a natural future extension, not built now --
keeps this pass a manageable size. All four trigger prices are therefore
always >= current_price by construction (a candidate just isn't returned
when nothing qualifying exists above price), and the shared "distance"
field is always price-to-trigger, never signed.

Comparison metric: raw DOLLAR distance to trigger, not percentage and
not volatility-relative. A percentage or ATR-relative metric would
already be doing exactly the kind of implicit normalizing this
codebase's scoring principle (specs.md section 3) rejects for
level-strength components -- picking "closest" should stay as legible as
"which price is nearest," not smuggle in a volatility model nobody
asked for. evaluate_setups() returns candidates already sorted ascending
by this distance -- the first element (if any) is "closest."

Migrated (phase 3.6 stage 3 part 2, specs.md section 19) off the
`bars`/`live_bars` split this module used to mirror from monitor-app/
state.py's now-retired `live_cadence_tail`: both `detect_levels` and
`evaluate_hold_time_aware` now see the SAME single `bars` list, the full
backfilled+live history -- `detect_levels` already did (a whole-session
scan), and `evaluate_hold_time_aware` correctly weights whatever cadence
each bar actually has instead of needing a pre-filtered uniform-cadence
subset (specs.md sections 15-17). `watch_added_ts`, threaded through to
every `evaluate_hold_time_aware` call here, guards the real risk this
migration introduced: a hold could otherwise complete ENTIRELY within
backfilled (pre-watch) bars, showing "confirmed" the instant a symbol is
added -- see `evaluate_hold_time_aware`'s own docstring and specs.md
section 19 for the full real-data finding and fix.
"""
from __future__ import annotations

from dataclasses import dataclass

from levels import Level, HoldStateTimeAware, detect_levels, evaluate_hold_time_aware, nearest_round_number_above

REQUIRED_HOLD_SECONDS = 30.0

# Half of detect_levels' own default swing_window (3), floored to an
# integer -- literally "the same function, called a second time with a
# shorter window," not an arbitrary distinct constant. A window of 1
# means only a single bar on each side has to be beaten, so it catches
# short-term micro-structure swings the main window=3 scan is too coarse
# to see, at the cost of more noise -- exactly the tradeoff "micro-
# breakout" implies versus the main resistance-breakout candidate.
MICRO_SWING_WINDOW = 1

# How close price has to be to session VWAP, as a fraction of VWAP, to
# count as "pulled back" rather than "already run away from it." 0.5% is
# deliberately tight -- a VWAP pullback is meant to be a shallow dip back
# to the volume-weighted average price is already trending above, not
# any old visit near it at some point in the session.
VWAP_PULLBACK_THRESHOLD_PCT = 0.005


@dataclass
class SetupCandidate:
    setup_type: str    # "resistance_breakout" | "micro_breakout" | "vwap_reclaim" | "round_number_reclaim"
    trigger_price: float
    distance: float     # dollar distance from current price to trigger_price -- always >= 0
    hold: dict          # same shape as monitor-app/state.py's existing level "hold" block
    factors: dict        # type-specific detail -- never collapsed into one score


def _hold_dict(hold: HoldStateTimeAware) -> dict:
    return {
        "direction": hold.direction,
        "required_seconds": REQUIRED_HOLD_SECONDS,
        "elapsed_seconds": hold.elapsed_seconds,
        "confirmed": hold.confirmed,
        "failed_attempts": hold.failed_attempts,
        # When `confirmed` was last genuinely reaffirmed (specs.md
        # section 20) -- None if never confirmed. journal_logic.py's
        # staleness gate reads this to decide whether a persisted
        # confirmed=True is still actionable for a NEW entry; it is
        # never used to alter `confirmed` itself, which stays exactly as
        # evaluate_hold_time_aware computed it.
        "confirmed_at_ts": hold.confirmed_at_ts,
    }


def _nearest_above(levels: list[Level], current_price: float) -> Level | None:
    """Strongest resistance level priced above current_price. Same
    picking logic as monitor-app/state.py's select_levels, reimplemented
    here rather than imported -- core/ must not depend on monitor-app/
    (AGENT_PROTOCOL.md's directory-boundary rule), and this is three
    lines, not a real duplication risk."""
    above = [l for l in levels if l.kind == "resistance" and l.price > current_price]
    return max(above, key=lambda l: l.strength_score) if above else None


def _breakout_candidate(setup_type: str, bars: list[dict],
                        current_price: float, swing_window: int,
                        watch_added_ts: float | None) -> SetupCandidate | None:
    """Shared implementation for both resistance_breakout (swing_window=3,
    the detect_levels default) and micro_breakout (swing_window=
    MICRO_SWING_WINDOW) -- same detect_levels function, same nearest-above
    picking, same evaluate_hold_time_aware call; only the window differs.
    No new detection logic for micro_breakout, exactly as specced."""
    levels = detect_levels(bars, swing_window=swing_window)
    level = _nearest_above(levels, current_price)
    if level is None:
        return None
    hold = evaluate_hold_time_aware(bars, level.price, direction="above",
                                    required_seconds=REQUIRED_HOLD_SECONDS,
                                    watch_added_ts=watch_added_ts)
    return SetupCandidate(
        setup_type=setup_type,
        trigger_price=round(level.price, 4),
        distance=round(level.price - current_price, 4),
        hold=_hold_dict(hold),
        factors={
            "strength_score": round(level.strength_score, 4),
            "touch_count": level.touch_count,
            "total_touch_volume": level.total_touch_volume,
            "round_number_bonus": round(level.round_number_bonus, 4),
        },
    )


def _vwap_reclaim_candidate(bars: list[dict], current_price: float,
                            vwap: float | None,
                            watch_added_ts: float | None) -> SetupCandidate | None:
    """Trend: current price at/above session VWAP (a simple instantaneous
    check, deliberately not a multi-bar trend model -- keeps this pass a
    manageable size, same spirit as the breakout-above-only scope).
    Pullback: price within VWAP_PULLBACK_THRESHOLD_PCT of VWAP. Reclaim:
    evaluate_hold_time_aware treating VWAP itself as the level to
    hold/reclaim closes above, same 30-second confirmation as everything
    else. Absent (not a zero/null candidate) when either condition
    doesn't hold -- same "not watchable right now" convention as the
    other three types."""
    if vwap is None or vwap <= 0:
        return None
    is_uptrend = current_price >= vwap
    distance_pct = abs(current_price - vwap) / vwap
    is_pullback = distance_pct <= VWAP_PULLBACK_THRESHOLD_PCT
    if not (is_uptrend and is_pullback):
        return None
    hold = evaluate_hold_time_aware(bars, vwap, direction="above",
                                    required_seconds=REQUIRED_HOLD_SECONDS,
                                    watch_added_ts=watch_added_ts)
    return SetupCandidate(
        setup_type="vwap_reclaim",
        trigger_price=round(vwap, 4),
        distance=round(current_price - vwap, 4),
        hold=_hold_dict(hold),
        factors={
            "vwap": round(vwap, 4),
            "distance_from_vwap_pct": round(distance_pct * 100.0, 4),
            "trend_is_above_vwap": is_uptrend,
        },
    )


def _round_number_reclaim_candidate(bars: list[dict], current_price: float,
                                    watch_added_ts: float | None) -> SetupCandidate | None:
    """The one type watchable even with ZERO prior price touches at that
    level -- an untested round number is still a psychologically real
    level to retail traders, unlike a swing level which requires an
    actual prior touch to exist at all (specs.md phase 3.5). Always
    present: there is always a next round-number grid point above any
    price, so unlike the other three types this one never comes back
    None."""
    trigger = nearest_round_number_above(current_price)
    hold = evaluate_hold_time_aware(bars, trigger, direction="above",
                                    required_seconds=REQUIRED_HOLD_SECONDS,
                                    watch_added_ts=watch_added_ts)
    return SetupCandidate(
        setup_type="round_number_reclaim",
        trigger_price=round(trigger, 4),
        distance=round(trigger - current_price, 4),
        hold=_hold_dict(hold),
        factors={
            "nearest_round_price": round(trigger, 4),
            "requires_prior_touches": False,
        },
    )


def evaluate_setups(
    bars: list[dict],
    current_price: float,
    vwap: float | None,
    main_swing_window: int = 3,
    watch_added_ts: float | None = None,
) -> list[SetupCandidate]:
    """All setup-type candidates currently watchable (breakout-above
    direction only -- see module docstring), sorted ascending by dollar
    distance to trigger; candidates[0] (if the list is non-empty) is
    "closest." A type that isn't watchable right now (no level above
    price, no real VWAP pullback in progress) is simply absent from the
    result, never a null/zero placeholder entry.

    `bars` is the FULL backfilled+live series (phase 3.6 stage 3 part 2 --
    the old `live_bars` split is retired, see module docstring).
    `watch_added_ts`, when given, is this symbol's own watch-start time
    (epoch seconds) -- passed straight through to every
    `evaluate_hold_time_aware` call so a hold can't show "confirmed" from
    purely pre-watch backfilled history (see that function's docstring)."""
    candidates = [
        _breakout_candidate("resistance_breakout", bars, current_price,
                            main_swing_window, watch_added_ts),
        _breakout_candidate("micro_breakout", bars, current_price,
                            MICRO_SWING_WINDOW, watch_added_ts),
        _vwap_reclaim_candidate(bars, current_price, vwap, watch_added_ts),
        _round_number_reclaim_candidate(bars, current_price, watch_added_ts),
    ]
    present = [c for c in candidates if c is not None]
    return sorted(present, key=lambda c: c.distance)
