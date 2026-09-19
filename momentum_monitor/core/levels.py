"""
Level detection and hold-confirmation.

This directly replaces two things diagnosed as broken in ToS_Companion:
1. "Nearest resistance" picking noise instead of a real level (no strength
   concept - every candidate level was treated as equally valid).
2. Entry firing on first-tick touch instead of a sustained hold (no
   confirmation concept at all).

Both fixes live here, together, because a level score and its hold-state are
tightly related: a level that's been tested and rejected twice should score
HIGHER (it's a real, defended level) while simultaneously requiring MORE
confirmation before trusting a break through it - not less. Keeping them
in one module makes that relationship visible instead of accidental.
"""
from __future__ import annotations
import math
from dataclasses import dataclass, field

# Round-number grid, TIERED by price -- retail attention clusters at
# round numbers, but what counts as "round" scales with price: a nickel
# matters at $1, but is meaningless noise at $150, while a half-dollar
# jump is far too coarse to mean anything for a $1 stock. A single fixed
# $0.50 grid (the original version of this) got that wrong at both ends.
# Tiers (see specs.md section 3 for the full reasoning): under $2, dimes
# ($0.10); $2 up to $10, quarters ($0.25); $10 and up, half-dollars
# ($0.50) -- chosen to roughly cover the user's stated $0.50-$15 trading
# range, with the two breakpoints ($2, $10) deliberately picked so they
# land on a shared multiple of every tier's increment on both sides (2.0
# is a multiple of both 0.10 and 0.25; 10.0 is a multiple of both 0.25
# and 0.50), so the grid has no discontinuity exactly at a boundary.
# Shared by both the proximity bonus below (nearest EITHER side, used to
# score an already-detected level) and setup_types.py's round-number
# reclaim candidate (nearest ABOVE only, since that candidate is
# breakout-above-direction-only) -- one canonical grid definition, not
# two independently-chosen ones.
def _round_number_increment(price: float) -> float:
    if price < 2.0:
        return 0.10
    if price < 10.0:
        return 0.25
    return 0.50


@dataclass
class Level:
    price: float
    kind: str  # "resistance" or "support"
    touch_count: int
    total_touch_volume: float
    last_touch_ts: int
    # Components are kept separately and NOT pre-averaged into one opaque
    # number without explanation - same principle as the readout design:
    # show what's driving the score, don't hide it.
    round_number_bonus: float
    strength_score: float


def _swing_points(bars: list[dict], window: int, kind: str) -> list[int]:
    """Indices of local swing highs (kind='high') or lows (kind='low').

    A bar with volume == 0 is never eligible as the CENTER of a swing
    point, even if its high/low ties the window's extreme. Real trades
    essentially never print zero shares, so a run of identical zero-volume
    bars is schwab-connector's own forward-fill for a quiet 10s bucket
    (aggregator.py's _fill_gap_until: open==high==low==close==prior close,
    volume=0.0), not repeated real price tests -- confirmed live (a
    resistance level showing touch_count=20 with total_touch_volume
    exactly 0). Neighboring zero-volume bars still count toward a REAL
    bar's own window comparison (seg_vals below) -- only candidacy as the
    touch itself is restricted, not the context used to judge one."""
    idxs = []
    for i in range(window, len(bars) - window):
        if bars[i]["volume"] == 0:
            continue
        seg = bars[i - window: i + window + 1]
        val = bars[i]["high"] if kind == "high" else bars[i]["low"]
        seg_vals = [b["high"] if kind == "high" else b["low"] for b in seg]
        if kind == "high" and val == max(seg_vals):
            idxs.append(i)
        elif kind == "low" and val == min(seg_vals):
            idxs.append(i)
    return idxs


def _walk_real_neighbors(bars: list[dict], i: int, step: int, multiple: float,
                         max_hop_seconds: float) -> list[dict]:
    """Walks outward from index `i` in `step` direction (-1 backward, +1
    forward), accumulating REAL elapsed time hop by hop, using each
    traversed pair's own actual gap -- never a scalar derived from the
    candidate alone (specs.md section 17: a single scalar, however
    derived, cannot distinguish "the local cadence here is genuinely
    coarse" from "there's a gap of the same magnitude" using only the
    bars immediately touching the candidate).

    A single hop larger than `max_hop_seconds` is a hard stop: never
    crossed, never counted, rather than treated as "far away but still
    valid, scaled generously." This is what actually prevents crossing a
    real gap, regardless of which bar's own width would otherwise have
    justified a wide window. The accumulation TARGET on this side is
    `multiple` times the FIRST (immediately adjacent) hop's own real
    size -- so it still scales to whatever local cadence genuinely
    exists right next to the candidate -- but `max_hop_seconds` applies
    to that first hop too, so a candidate sitting immediately next to a
    genuine gap can never use the gap itself to inflate its own target.
    """
    j = i
    nxt = j + step
    if not (0 <= nxt < len(bars)):
        return []
    first_hop = abs(bars[nxt]["ts"] - bars[j]["ts"])
    if first_hop > max_hop_seconds:
        return []
    target = multiple * first_hop
    accumulated = 0.0
    collected: list[dict] = []
    while 0 <= nxt < len(bars):
        hop = abs(bars[nxt]["ts"] - bars[j]["ts"])
        if hop > max_hop_seconds:
            break
        accumulated += hop
        collected.append(bars[nxt])
        if accumulated >= target:
            break
        j = nxt
        nxt = j + step
    return collected


def swing_points_time_aware(bars: list[dict], kind: str, multiple: float = 3.0,
                            max_hop_seconds: float = 90.0) -> list[int]:
    """Time-aware analog of `_swing_points` (specs.md sections 15-17,
    phase 3.6): a candidate is compared against every bar reachable by a
    real, cadence-adaptive TWO-DIRECTIONAL WALK on each side (see
    `_walk_real_neighbors`), rather than a fixed bar count or any single
    real-time span derived from the candidate alone.

    An earlier version derived one window from `multiple *` the
    candidate's own single observed width (`_bar_duration`, the gap to
    its NEXT bar). That closed the original fixed-window bug (a 30s
    window too narrow for 60s backfill cadence) but, checked against the
    FULL real AIFF/AEMD/DAIC/DTSS capture rather than the one instance
    originally found, turned out to have two STRUCTURAL blind spots: 54
    real bars where a narrow forward gap masked a genuinely wider real
    backward neighbor (a bar right at a cadence speed-up), and 63 of 100
    real large-gap bars where an inflated forward gap let the window
    bridge back across the gap. Neither `min` nor `max` of the two
    neighboring gaps closed both (verified against the same real data,
    not just reasoned about): `min` changed nothing (forward was already
    the smaller value in the vast majority of real cases), `max` fixed
    the narrow-window blind spot completely but made gap-bridging WORSE
    (100/100 instead of 63/100) -- a genuine, unavoidable tension in any
    design deriving one scalar per side from a single adjacent gap.

    The two-directional walk here resolves both (verified against the
    same real data: 0 remaining narrow-window blind spots, 0/100
    remaining gap-bridges at `max_hop_seconds=90.0`, chosen because it
    sits cleanly between real backfill's 60s baseline cadence and the
    smallest real "skipped minute" gap observed, 120s). Real tradeoff,
    found and reported rather than hidden: this is more conservative
    than the single-scalar design even in ordinary sparse (100-180s)
    stretches -- 25/26 real swing points found in AIFF's real backfilled
    portion, vs. that design's 44/39 -- but that higher count was itself
    partly inflated by the very gap-bridging this version closes, and
    25/26 is still a substantial real improvement over the original
    fixed-window design's 0/0 there.

    The zero-volume forward-fill exclusion (see `_swing_points`'s
    docstring) is preserved unchanged -- that rule is about real vs.
    synthetic bars, orthogonal to windowing strategy entirely.
    """
    idxs = []
    for i, cand in enumerate(bars):
        if cand["volume"] == 0:
            continue
        before = _walk_real_neighbors(bars, i, -1, multiple, max_hop_seconds)
        after = _walk_real_neighbors(bars, i, +1, multiple, max_hop_seconds)
        if not before or not after:
            continue
        seg = before + [cand] + after
        val = cand["high"] if kind == "high" else cand["low"]
        seg_vals = [b["high"] if kind == "high" else b["low"] for b in seg]
        if kind == "high" and val == max(seg_vals):
            idxs.append(i)
        elif kind == "low" and val == min(seg_vals):
            idxs.append(i)
    return idxs


def confirmed_swing_lows(bars: list[dict], window: int = 3) -> list[dict]:
    """Every CONFIRMED swing low in `bars` (a local minimum with `window`
    bars fully bracketing it on both sides, per `_swing_points` -- reused
    directly, not reimplemented) as `{"ts", "price"}` dicts, oldest first.

    Exposed as its own public function, separate from `detect_levels`'
    clustered/scored `Level` output, because the virtual journal's early-
    phase exit (specs.md section 12, the swing-low-anchored stop) needs
    the raw sequence of confirmed lows -- including the SAME real
    confirmation delay `_swing_points` already imposes (a low isn't
    "confirmed" until `window` bars have printed after it) -- not a level
    clustered and scored for resistance/support display. `bars` is used
    exactly as given; a caller wanting "since a position's entry" slices
    to that range itself, the same way every other function in this
    module takes bars as-is with no concept of a caller-specific window."""
    idxs = _swing_points(bars, window, kind="low")
    return [{"ts": bars[i]["ts"], "price": bars[i]["low"]} for i in idxs]


def _nearest_round_number(price: float, increment: float | None = None) -> float:
    """Nearest round-number grid point on EITHER side of `price`. Grid
    increment is picked by `_round_number_increment(price)` (that price's
    own tier) unless the caller overrides it explicitly."""
    inc = increment if increment is not None else _round_number_increment(price)
    return round(price / inc) * inc


def nearest_round_number_above(price: float, increment: float | None = None) -> float:
    """Smallest round-number grid point STRICTLY ABOVE `price`, on the
    grid tier `price` itself falls into (see `_round_number_increment`).
    Used by setup_types.py's round-number reclaim candidate: that setup
    type is breakout-above-direction-only (specs.md phase 3.5's
    deliberate scope for this pass), so the relevant round level is
    always the next one up, never merely the nearest in either direction
    the way the bonus below needs. The while-loop is a float-precision
    guard (price could land fractionally below an increment boundary due
    to float representation, e.g. 9.0 stored as 8.999999999999998), not
    expected to loop more than once in practice."""
    inc = increment if increment is not None else _round_number_increment(price)
    candidate = (math.floor(price / inc) + 1) * inc
    while candidate <= price:
        candidate += inc
    return round(candidate, 4)


def _round_number_bonus(price: float) -> float:
    """Small bonus for proximity to a round-number grid point (tiered by
    price -- see `_round_number_increment`) - retail attention tends to
    cluster there, especially in low-priced names."""
    nearest = _nearest_round_number(price)
    distance_pct = abs(price - nearest) / price
    return max(0.0, 1.0 - distance_pct / 0.01)  # full bonus within 1%, fades to 0


def detect_levels(
    bars: list[dict],
    swing_window: int = 3,
    cluster_tolerance_pct: float = 0.006,
) -> list[Level]:
    """
    Finds swing highs/lows, clusters nearby ones into levels, and scores each
    by touch count, volume concentration, and round-number proximity.
    Deliberately NOT scored by recency-only "nearest to current price" -
    that's the exact behavior that let noise through before.
    """
    levels: list[Level] = []
    for kind, point_kind in (("resistance", "high"), ("support", "low")):
        idxs = _swing_points(bars, swing_window, point_kind)
        touches = [
            (bars[i]["high"] if kind == "resistance" else bars[i]["low"], bars[i])
            for i in idxs
        ]
        touches.sort(key=lambda t: t[0])

        clusters: list[list[tuple[float, dict]]] = []
        for price, bar in touches:
            placed = False
            for cluster in clusters:
                cluster_avg = sum(p for p, _ in cluster) / len(cluster)
                if abs(price - cluster_avg) / cluster_avg <= cluster_tolerance_pct:
                    cluster.append((price, bar))
                    placed = True
                    break
            if not placed:
                clusters.append([(price, bar)])

        for cluster in clusters:
            avg_price = sum(p for p, _ in cluster) / len(cluster)
            touch_count = len(cluster)
            total_vol = sum(b["volume"] for _, b in cluster)
            last_ts = max(b["ts"] for _, b in cluster)
            bonus = _round_number_bonus(avg_price)

            # Explicit, legible weights - not learned, not hidden. Touches
            # matter most (a level that's been defended repeatedly is the
            # strongest signal); volume and round-number proximity are
            # secondary contributors.
            strength = touch_count * 2.0 + (total_vol / 1_000_000) * 0.5 + bonus

            levels.append(Level(
                price=avg_price, kind=kind, touch_count=touch_count,
                total_touch_volume=total_vol, last_touch_ts=last_ts,
                round_number_bonus=bonus, strength_score=strength,
            ))

    return sorted(levels, key=lambda l: l.strength_score, reverse=True)


@dataclass
class HoldState:
    level_price: float
    direction: str  # "above" or "below"
    consecutive_bars: int
    confirmed: bool
    failed_attempts: int = 0


def evaluate_hold(
    bars: list[dict],
    level_price: float,
    direction: str = "above",
    required_bars: int = 3,
) -> HoldState:
    """
    Walks the bar sequence and tracks consecutive CLOSES on the required
    side of the level - not touches, not wicks. A close back on the wrong
    side resets the streak and counts as a failed attempt. This is the
    entry-side confirmation logic; it must never be applied to stop-loss
    evaluation, which should stay immediate and unconditional.
    """
    consecutive = 0
    failed_attempts = 0
    confirmed = False
    was_attempting = False

    for b in bars:
        on_side = b["close"] > level_price if direction == "above" else b["close"] < level_price
        if on_side:
            consecutive += 1
            was_attempting = True
            if consecutive >= required_bars:
                confirmed = True
        else:
            if was_attempting and consecutive > 0 and not confirmed:
                failed_attempts += 1
            consecutive = 0
            was_attempting = False
            # Once confirmed, a single close back through doesn't retroactively
            # un-confirm history - it would be reflected as a new level
            # interaction on the next call with fresh bars.

    return HoldState(
        level_price=level_price, direction=direction,
        consecutive_bars=consecutive, confirmed=confirmed,
        failed_attempts=failed_attempts,
    )


@dataclass
class HoldStateTimeAware:
    level_price: float
    direction: str  # "above" or "below"
    elapsed_seconds: float
    confirmed: bool
    failed_attempts: int = 0


def evaluate_hold_time_aware(
    bars: list[dict],
    level_price: float,
    direction: str = "above",
    required_seconds: float = 30.0,
    reference_interval_seconds: float = 10.0,
) -> HoldStateTimeAware:
    """Time-aware `evaluate_hold` (specs.md section 15/16, phase 3.6):
    tracks real ELAPSED SECONDS on the required side of the level, rather
    than a bar count. `required_bars=3` at the live 10s cadence is
    `required_seconds=30.0` (3 * 10s) -- confirmation still fires on
    exactly the 3rd bar on uniform cadence (proved in specs.md section 15
    by direct substitution, and covered by
    test_evaluate_hold_time_aware_exactly_equals_bar_count_version_on_uniform_cadence).

    Boundary, made explicit (this was left implicit in the bar-count
    version): elapsed time is measured as of the END of a bar's own
    interval, not its start timestamp -- a bar's own width counts in full
    toward the streak once that bar closes on the right side. A bar's own
    width is `bars[i+1]["ts"] - bars[i]["ts"]` when a next bar exists
    (the bar's REAL observed width, whatever it actually was -- a 60s
    backfilled bar counts as 60s, not a fixed assumption), or
    `reference_interval_seconds` for the newest/last bar in the list,
    where no next bar exists yet to measure from (specs.md section 16:
    stage 1 used a FIXED `reference_interval_seconds` for every bar,
    which only happened to be correct because every stage-1 test used
    uniform 10s bars -- section 16 shows this over-counts real elapsed
    time, and can wrongly confirm early, once bar widths actually vary).
    This is why `required_seconds=30.0`, not `20.0`, on uniform data --
    "3 consecutive bars" means 3 FULL bar-widths of confirmed time, not
    the gap between the 1st and 3rd bar starts.
    """
    streak_start_ts = None
    failed_attempts = 0
    confirmed = False
    elapsed_seconds = 0.0
    was_attempting = False

    for i, b in enumerate(bars):
        on_side = b["close"] > level_price if direction == "above" else b["close"] < level_price
        if on_side:
            if streak_start_ts is None:
                streak_start_ts = b["ts"]
            was_attempting = True
            bar_end_ts = bars[i + 1]["ts"] if i + 1 < len(bars) else b["ts"] + reference_interval_seconds
            elapsed_seconds = bar_end_ts - streak_start_ts
            if elapsed_seconds >= required_seconds:
                confirmed = True
        else:
            if was_attempting and streak_start_ts is not None and not confirmed:
                failed_attempts += 1
            streak_start_ts = None
            elapsed_seconds = 0.0
            was_attempting = False

    return HoldStateTimeAware(
        level_price=level_price, direction=direction,
        elapsed_seconds=elapsed_seconds, confirmed=confirmed,
        failed_attempts=failed_attempts,
    )
