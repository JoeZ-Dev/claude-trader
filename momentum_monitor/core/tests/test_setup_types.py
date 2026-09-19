import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from setup_types import evaluate_setups
from levels import nearest_round_number_above


def bar(ts, o, h, l, c, v):
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}


def _double_top_bars(peak=8.69):
    """Same double-top shape as core/tests/test_core.py's detect_levels
    test -- two swing highs at ~8.69, far enough apart to be distinct
    swing points, close enough in price to cluster into one resistance
    level. Reused here (not re-derived) so the resistance-breakout
    candidate is checked against the exact same known-good fixture the
    base detect_levels behavior is already proven against.

    Extended with 3 trailing padding bars versus test_core.py's version:
    that test passes swing_window=2 explicitly, but evaluate_setups uses
    the production default (3), whose valid index range (range(window,
    len-window)) would otherwise exclude the second peak entirely --
    padding keeps both peaks in range at the default window too."""
    bars = []
    ts = 0
    for p in [7.0, 7.5, 8.2, peak, 8.0, 7.6]:
        bars.append(bar(ts, p, p + 0.05, p - 0.05, p, 50_000)); ts += 60
    for p in [7.0, 6.9, 7.1]:
        bars.append(bar(ts, p, p + 0.05, p - 0.05, p, 30_000)); ts += 60
    for p in [7.4, 7.9, 8.3, peak - 0.04, 7.9, 7.6]:
        bars.append(bar(ts, p, p + 0.05, p - 0.05, p, 60_000)); ts += 60
    for p in [7.5, 7.4, 7.3]:
        bars.append(bar(ts, p, p + 0.05, p - 0.05, p, 40_000)); ts += 60
    return bars


# -- resistance breakout (existing detect_levels + evaluate_hold, reused) --

def test_resistance_breakout_candidate_matches_known_double_top_level():
    bars = _double_top_bars()
    current_price = 8.0  # below the ~8.69 double top
    candidates = evaluate_setups(bars, current_price, vwap=None)
    resistance = next(c for c in candidates if c.setup_type == "resistance_breakout")
    assert 8.5 < resistance.trigger_price < 8.75
    assert resistance.factors["touch_count"] == 2
    assert resistance.distance == round(resistance.trigger_price - current_price, 4)
    assert resistance.hold["direction"] == "above"
    assert resistance.hold["required_seconds"] == 30.0


def test_resistance_breakout_absent_when_nothing_above_price():
    bars = _double_top_bars()
    candidates = evaluate_setups(bars, current_price=100.0, vwap=None)
    assert not any(c.setup_type == "resistance_breakout" for c in candidates)


# -- micro-breakout (same detect_levels, shorter window, no new logic) --

def test_micro_breakout_finds_a_level_the_main_window_misses():
    # Only 5 bars -- detect_levels' swing_window=3 default needs at least
    # 7 (range(window, len-window) is empty here), so the main
    # resistance-breakout candidate can't exist at all. MICRO_SWING_WINDOW
    # =1 only needs 3, so it still finds the single swing high at index 2.
    bars = [
        bar(0, 7.0, 7.05, 6.95, 7.0, 50_000),
        bar(60, 7.3, 7.35, 7.25, 7.3, 50_000),
        bar(120, 7.6, 7.65, 7.55, 7.6, 50_000),
        bar(180, 7.4, 7.45, 7.35, 7.4, 50_000),
        bar(240, 7.1, 7.15, 7.05, 7.1, 50_000),
    ]
    current_price = 7.0
    candidates = evaluate_setups(bars, current_price, vwap=None)
    types = {c.setup_type for c in candidates}
    assert "resistance_breakout" not in types
    micro = next(c for c in candidates if c.setup_type == "micro_breakout")
    assert abs(micro.trigger_price - 7.65) < 1e-9
    assert micro.factors["touch_count"] == 1


# -- VWAP pullback-reclaim (trend + pullback gate, then evaluate_hold) --

def test_vwap_reclaim_candidate_present_on_a_real_pullback():
    vwap = 10.0
    live_bars = [
        bar(0, 9.9, 10.05, 9.85, 10.02, 20_000),
        bar(60, 10.02, 10.15, 9.95, 10.08, 25_000),
        bar(120, 10.08, 10.2, 10.0, 10.15, 22_000),
    ]
    current_price = 10.03  # 0.3% above vwap -- inside the pullback threshold
    candidates = evaluate_setups(live_bars, current_price, vwap=vwap)
    reclaim = next(c for c in candidates if c.setup_type == "vwap_reclaim")
    assert reclaim.trigger_price == 10.0
    assert reclaim.distance == round(current_price - vwap, 4)
    assert reclaim.factors["trend_is_above_vwap"] is True


def test_vwap_reclaim_absent_when_price_has_run_away_from_vwap():
    vwap = 10.0
    live_bars = [bar(0, 10.5, 10.6, 10.4, 10.5, 10_000)]
    candidates = evaluate_setups(live_bars, current_price=10.5, vwap=vwap)
    assert not any(c.setup_type == "vwap_reclaim" for c in candidates)


def test_vwap_reclaim_absent_when_price_below_vwap_not_an_uptrend():
    vwap = 10.0
    live_bars = [bar(0, 9.9, 9.95, 9.8, 9.85, 10_000)]
    candidates = evaluate_setups(live_bars, current_price=9.98, vwap=vwap)
    assert not any(c.setup_type == "vwap_reclaim" for c in candidates)


# -- round-number reclaim (the genuinely new behavior: zero touches ok) --

def test_round_number_reclaim_watchable_with_zero_prior_touches():
    # Monotonic bars, no swing points anywhere -- detect_levels would
    # find NOTHING here, yet round-number reclaim must still compute a
    # valid candidate. This is the one genuinely new behavior versus
    # everything built before phase 3.5 (specs.md): watchable even with
    # zero real price history at the level itself.
    live_bars = [bar(i * 10, 24.5 + i * 0.02, 24.55 + i * 0.02, 24.45 + i * 0.02,
                     24.5 + i * 0.02, 10_000) for i in range(5)]
    current_price = 24.7
    candidates = evaluate_setups(live_bars, current_price, vwap=None)
    reclaim = next(c for c in candidates if c.setup_type == "round_number_reclaim")
    assert reclaim.trigger_price == 25.0
    assert reclaim.factors["requires_prior_touches"] is False
    assert reclaim.distance == round(25.0 - 24.7, 4)


def test_nearest_round_number_above_always_strictly_greater():
    assert nearest_round_number_above(24.7) == 25.0
    assert nearest_round_number_above(25.0) == 25.5  # exactly on the grid -- next one up, not itself


def test_nearest_round_number_above_uses_the_tier_for_price():
    # Under $2: dimes.
    assert nearest_round_number_above(0.02) == 0.10
    assert nearest_round_number_above(1.23) == 1.30
    # $2 up to $10: quarters.
    assert nearest_round_number_above(2.05) == 2.25
    assert nearest_round_number_above(5.10) == 5.25
    # $10 and up: half-dollars (the pre-tiering behavior, unchanged).
    assert nearest_round_number_above(10.01) == 10.5
    assert nearest_round_number_above(153.6) == 154.0


def test_nearest_round_number_above_has_no_discontinuity_at_tier_boundaries():
    # The dime grid reaching $2 must land exactly on $2.00 (also a valid
    # quarter-grid point) -- no gap or overlap where the tiers meet.
    assert nearest_round_number_above(1.99) == 2.00
    # Same for the quarter grid reaching $10 (also a valid half-dollar
    # grid point).
    assert nearest_round_number_above(9.99) == 10.00


def test_round_number_reclaim_uses_the_dime_tier_for_a_sub_two_dollar_symbol():
    # A fixed $0.50 grid (the pre-tiered behavior) would put the trigger
    # a full 43 cents away here -- a huge, meaningless jump for a stock
    # trading under $2. The dime tier must be what actually surfaces
    # end to end through evaluate_setups, not just the standalone helper.
    live_bars = [bar(0, 1.05, 1.1, 1.0, 1.07, 10_000)]
    candidates = evaluate_setups(live_bars, current_price=1.07, vwap=None)
    reclaim = next(c for c in candidates if c.setup_type == "round_number_reclaim")
    assert reclaim.trigger_price == 1.10
    assert reclaim.distance == round(1.10 - 1.07, 4)


# -- dollar-distance sort ---------------------------------------------------

def test_candidates_sorted_ascending_by_dollar_distance_closest_first():
    bars = _double_top_bars()
    current_price = 8.02
    vwap = 8.0  # 0.25% away -- the closest trigger by construction
    candidates = evaluate_setups(bars, current_price, vwap=vwap)
    assert len(candidates) >= 2
    distances = [c.distance for c in candidates]
    assert distances == sorted(distances)
    assert candidates[0].setup_type == "vwap_reclaim"


def test_dollar_distance_identifies_closest_among_known_exact_values():
    current_price = 24.70
    vwap = 24.65  # 0.05 away -- closer than the round-number trigger at 25.0
    live_bars = [bar(0, 24.6, 24.68, 24.55, 24.65, 10_000)]
    candidates = evaluate_setups(live_bars, current_price, vwap=vwap)
    assert [c.setup_type for c in candidates] == ["vwap_reclaim", "round_number_reclaim"]
    assert abs(candidates[0].distance - 0.05) < 1e-9
    assert abs(candidates[1].distance - 0.3) < 1e-9


# -- watch_added_ts propagation (phase 3.6 stage 3 part 2, specs.md -------
# section 19) -- must reach every setup type's evaluate_hold_time_aware
# call, not just be accepted and silently dropped.

def test_watch_added_ts_prevents_confirming_purely_from_pre_watch_bars():
    # A round-number reclaim that would otherwise confirm entirely within
    # bars from before watch_added_ts must NOT show confirmed=True the
    # instant the symbol is watched -- the real risk this migration
    # introduced (specs.md section 19).
    bars = [
        bar(0, 1.1, 1.15, 1.05, 1.12, 10_000),
        bar(10, 1.12, 1.16, 1.08, 1.13, 10_000),
        bar(20, 1.12, 1.16, 1.08, 1.14, 10_000),  # confirms here if unrestricted (elapsed=30)
    ]
    current_price = 1.05  # nearest_round_number_above(1.05) == 1.10, below all three closes
    without_restriction = evaluate_setups(bars, current_price, vwap=None)
    reclaim = next(c for c in without_restriction if c.setup_type == "round_number_reclaim")
    assert reclaim.hold["confirmed"] is True  # the real risk, reproduced

    with_restriction = evaluate_setups(bars, current_price, vwap=None, watch_added_ts=1_000_000)
    reclaim_restricted = next(c for c in with_restriction if c.setup_type == "round_number_reclaim")
    assert reclaim_restricted.hold["confirmed"] is False


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
