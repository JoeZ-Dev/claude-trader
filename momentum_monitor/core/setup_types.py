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

from levels import (
    Level, HoldStateTimeAware, detect_levels, evaluate_hold_time_aware,
    nearest_round_number_above, nearest_round_number_below,
)

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

# Breakdown-candidate RELEVANCE (specs.md section 31 -- the "bearish
# signals reads as an overall verdict" framing fix): a real detected
# level is always returned (never hidden -- the underlying detection
# stays legitimate information), but round_number_breakdown is
# structurally ALWAYS present (there is always a next round-number grid
# point below any price) and support_breakdown/micro_breakdown are
# present for nearly any symbol with enough bar history, REGARDLESS of
# whether the level has anything to do with the CURRENT trend -- so
# "is_relevant" below is a separate signal the display layer uses to
# de-emphasize a level that's neither close by nor recently in play,
# rather than showing every breakdown candidate with identical visual
# weight no matter the context.
#
# BREAKDOWN_NEAR_DISTANCE_PCT: how close a level's trigger price has to
# be to current price, as a fraction of current price, to count as
# structurally "near" on its own. Deliberately a much LOOSER threshold
# than VWAP_PULLBACK_THRESHOLD_PCT above (0.5%) -- that constant defines
# a genuine, tight pullback CONDITION for a setup to exist at all; this
# one is a broader "close enough to plausibly matter soon" cutoff for a
# level that already exists, a different purpose, not reused blindly.
BREAKDOWN_NEAR_DISTANCE_PCT = 0.05

# BREAKDOWN_RECENT_TOUCH_SECONDS: how recently (real elapsed seconds) a
# level's last REAL touch has to have been to count as "recently
# tested," regardless of current distance -- a level price is actively
# drifting toward and testing reads very differently from one nobody's
# come near in hours. 30 minutes is long enough to not flag "touched
# once, ages ago, at the very start of the session" as still relevant,
# short enough to still mean "this is part of what's actually happening
# right now," not just theoretical structure. Only meaningful for a
# level with REAL touch history (support_breakdown/micro_breakdown) --
# round_number_breakdown has no such history by construction
# (requires_prior_touches=False), so distance is its only signal.
BREAKDOWN_RECENT_TOUCH_SECONDS = 1800.0

# BREAKDOWN_MOVED_AWAY_PCT: the directional gap "recently touched" alone
# missed (found live against a real TOPS breakout, ~$0.70 -> $1.18): a
# level "touched" 740s ago only because price ROCKETED straight through
# it on the way up is not "genuinely still in play" the way a level
# price is hovering near or retesting is -- confirmed on the real TOPS
# data, whose touch bar itself moved from a low of 0.718 to a close of
# 1.03 (43% within that ONE bar), with price 50% above the touch by the
# time it's evaluated. "Recently touched" only counts toward relevance
# when price hasn't ALSO moved more than this fraction away from the
# actual touch price (the touch bar's own low, for a support-kind level)
# since -- a SIGNED comparison, so price moving back TOWARD the level
# since the touch never fails this check, only moving further away does.
# 15% is deliberately looser than BREAKDOWN_NEAR_DISTANCE_PCT (5%) -- a
# level touched minutes ago with price still roughly in the same
# neighborhood should still read as active, not just literally
# unchanged; it's the DIRECTION and MAGNITUDE of a decisive move away,
# not any drift at all, that disqualifies a recent touch.
BREAKDOWN_MOVED_AWAY_PCT = 0.15


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


def _nearest_below(levels: list[Level], current_price: float) -> Level | None:
    """Strongest support level priced below current_price -- the floor
    mirror of `_nearest_above` (specs.md section 22's breakdown-below
    setup variants). Same picking logic as monitor-app/state.py's
    select_levels."""
    below = [l for l in levels if l.kind == "support" and l.price < current_price]
    return max(below, key=lambda l: l.strength_score) if below else None


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


# -- Breakdown-below variants (specs.md section 22) -------------------------
#
# Exact downside mirrors of the four functions above -- same detect_levels/
# evaluate_hold_time_aware primitives, same required-hold duration, same
# "absent, not a null placeholder, when not currently watchable" contract.
# INFORMATIONAL / WARNING SIGNALS ONLY: evaluate_breakdown_setups() is a
# completely SEPARATE function from evaluate_setups() above, returning a
# completely separate list -- never merged into it, never passed to
# monitor-app/journal_logic.py's should_enter/advance_journal (which only
# ever receive evaluate_setups()' own bullish-only list; see
# monitor-app/state.py's build_state and app.py's _update_journal). There
# is no code path by which a breakdown candidate can reach entry-decision
# logic -- it is a structurally separate list, not a filtered view of one
# shared list. journal_logic.py additionally never accepts a "breakdown
# setups" parameter of any kind (audited, specs.md section 22) and
# _first_newly_confirmed only ever considers setup_type strings from an
# explicit bullish allowlist -- defense in depth on top of the structural
# separation here, not the only thing preventing a breakdown type from
# ever firing a trade.

def _breakdown_candidate(setup_type: str, bars: list[dict],
                         current_price: float, swing_window: int,
                         watch_added_ts: float | None) -> SetupCandidate | None:
    """Floor mirror of `_breakout_candidate`: support_breakdown
    (swing_window=3) and micro_breakdown (swing_window=MICRO_SWING_
    WINDOW) share this, exactly the same way resistance_breakout/
    micro_breakout share `_breakout_candidate` above."""
    levels = detect_levels(bars, swing_window=swing_window)
    level = _nearest_below(levels, current_price)
    if level is None:
        return None
    hold = evaluate_hold_time_aware(bars, level.price, direction="below",
                                    required_seconds=REQUIRED_HOLD_SECONDS,
                                    watch_added_ts=watch_added_ts)
    distance = round(current_price - level.price, 4)
    distance_pct = abs(distance) / current_price if current_price > 0 else 0.0
    seconds_since_touch = bars[-1]["ts"] - level.last_touch_ts
    # The touch bar's own LOW is the actual value that registered as a
    # support-kind touch (detect_levels clusters on bars[i]["low"] for
    # kind="support") -- comparing CURRENT price to THAT, not to
    # level.price itself, measures whether price has moved away since
    # the touch actually happened, not just how far the level is now.
    touch_bar = next((b for b in bars if b["ts"] == level.last_touch_ts), None)
    touch_price = touch_bar["low"] if touch_bar is not None else level.price
    moved_away_pct = (current_price - touch_price) / touch_price if touch_price > 0 else 0.0
    recently_tested_and_still_in_play = (
        seconds_since_touch <= BREAKDOWN_RECENT_TOUCH_SECONDS
        and moved_away_pct <= BREAKDOWN_MOVED_AWAY_PCT
    )
    is_relevant = distance_pct <= BREAKDOWN_NEAR_DISTANCE_PCT or recently_tested_and_still_in_play
    return SetupCandidate(
        setup_type=setup_type,
        trigger_price=round(level.price, 4),
        distance=distance,
        hold=_hold_dict(hold),
        factors={
            "strength_score": round(level.strength_score, 4),
            "touch_count": level.touch_count,
            "total_touch_volume": level.total_touch_volume,
            "round_number_bonus": round(level.round_number_bonus, 4),
            "distance_pct": round(distance_pct * 100.0, 2),
            "seconds_since_last_touch": seconds_since_touch,
            "moved_away_pct_since_touch": round(moved_away_pct * 100.0, 2),
            "is_relevant": is_relevant,
        },
    )


def _vwap_breakdown_candidate(bars: list[dict], current_price: float,
                              vwap: float | None,
                              watch_added_ts: float | None) -> SetupCandidate | None:
    """Floor mirror of `_vwap_reclaim_candidate`: trend is price AT/BELOW
    session VWAP (a downtrend), pullback is a relief rally UP toward VWAP
    within the same `VWAP_PULLBACK_THRESHOLD_PCT`, and the "breakdown" is
    `evaluate_hold_time_aware` treating VWAP as the level price needs to
    hold BELOW (a rejection back down, not a reclaim back up)."""
    if vwap is None or vwap <= 0:
        return None
    is_downtrend = current_price <= vwap
    distance_pct = abs(current_price - vwap) / vwap
    is_pullback = distance_pct <= VWAP_PULLBACK_THRESHOLD_PCT
    if not (is_downtrend and is_pullback):
        return None
    hold = evaluate_hold_time_aware(bars, vwap, direction="below",
                                    required_seconds=REQUIRED_HOLD_SECONDS,
                                    watch_added_ts=watch_added_ts)
    return SetupCandidate(
        setup_type="vwap_breakdown",
        trigger_price=round(vwap, 4),
        distance=round(vwap - current_price, 4),
        hold=_hold_dict(hold),
        factors={
            "vwap": round(vwap, 4),
            "distance_from_vwap_pct": round(distance_pct * 100.0, 4),
            "trend_is_below_vwap": is_downtrend,
            # This setup's own gating (a genuine downtrend AND a shallow
            # pullback within VWAP_PULLBACK_THRESHOLD_PCT, both already
            # required above just to exist) already IS a relevance
            # filter -- it never appears as a stale/far candidate in the
            # first place, so it's always relevant when present.
            "is_relevant": True,
        },
    )


def _round_number_breakdown_candidate(bars: list[dict], current_price: float,
                                      watch_added_ts: float | None) -> SetupCandidate | None:
    """Floor mirror of `_round_number_reclaim_candidate`:
    `nearest_round_number_below()` instead of `..._above()`. Always
    present, same reasoning as the reclaim version -- there is always a
    next round-number grid point below any positive price."""
    trigger = nearest_round_number_below(current_price)
    hold = evaluate_hold_time_aware(bars, trigger, direction="below",
                                    required_seconds=REQUIRED_HOLD_SECONDS,
                                    watch_added_ts=watch_added_ts)
    distance_pct = abs(current_price - trigger) / current_price if current_price > 0 else 0.0
    return SetupCandidate(
        setup_type="round_number_breakdown",
        trigger_price=round(trigger, 4),
        distance=round(current_price - trigger, 4),
        hold=_hold_dict(hold),
        factors={
            "nearest_round_price": round(trigger, 4),
            "requires_prior_touches": False,
            "distance_pct": round(distance_pct * 100.0, 2),
            # No real touch history to be "recently tested" by (a round
            # number is a grid point, not a detected level) -- distance
            # is the ONLY relevance signal available for this type.
            "is_relevant": distance_pct <= BREAKDOWN_NEAR_DISTANCE_PCT,
        },
    )


def evaluate_breakdown_setups(
    bars: list[dict],
    current_price: float,
    vwap: float | None,
    main_swing_window: int = 3,
    watch_added_ts: float | None = None,
) -> list[SetupCandidate]:
    """All FOUR breakdown-below candidates currently watchable, sorted
    ascending by dollar distance to trigger -- the exact downside mirror
    of `evaluate_setups()`, and STRUCTURALLY SEPARATE from it (see this
    section's module-level comment above): this function's output must
    never be merged into `evaluate_setups()`'s list or passed to
    monitor-app/journal_logic.py's should_enter/advance_journal. These
    are warning/context signals for the user's own judgment, never a
    trade trigger -- `setup_type` strings ("support_breakdown",
    "micro_breakdown", "vwap_breakdown", "round_number_breakdown") are
    deliberately distinct from all four bullish ones so the two lists
    can never be confused even if ever accidentally concatenated
    somewhere downstream."""
    candidates = [
        _breakdown_candidate("support_breakdown", bars, current_price,
                             main_swing_window, watch_added_ts),
        _breakdown_candidate("micro_breakdown", bars, current_price,
                             MICRO_SWING_WINDOW, watch_added_ts),
        _vwap_breakdown_candidate(bars, current_price, vwap, watch_added_ts),
        _round_number_breakdown_candidate(bars, current_price, watch_added_ts),
    ]
    present = [c for c in candidates if c is not None]
    return sorted(present, key=lambda c: c.distance)
