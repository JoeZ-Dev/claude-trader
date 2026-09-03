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
from dataclasses import dataclass, field


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
    """Indices of local swing highs (kind='high') or lows (kind='low')."""
    idxs = []
    for i in range(window, len(bars) - window):
        seg = bars[i - window: i + window + 1]
        val = bars[i]["high"] if kind == "high" else bars[i]["low"]
        seg_vals = [b["high"] if kind == "high" else b["low"] for b in seg]
        if kind == "high" and val == max(seg_vals):
            idxs.append(i)
        elif kind == "low" and val == min(seg_vals):
            idxs.append(i)
    return idxs


def _round_number_bonus(price: float) -> float:
    """Small bonus for proximity to a half-dollar/dollar level - retail
    attention tends to cluster there, especially in low-priced names."""
    nearest_half = round(price * 2) / 2
    distance_pct = abs(price - nearest_half) / price
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
