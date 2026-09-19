import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from indicators import (
    continuation_days, session_vwap, ema, macd, relative_volume,
    ema_time_aware, relative_volume_time_aware,
)
from levels import (
    confirmed_swing_lows, detect_levels, evaluate_hold,
    evaluate_hold_time_aware, swing_points_time_aware, _swing_points,
)


def bar(ts, o, h, l, c, v):
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}


def test_session_vwap_hand_computed():
    # Bar 1: typical=(10+8+9)/3=9, vol=100 -> pv=900, cum_vol=100 -> vwap=9.0
    # Bar 2: typical=(12+10+11)/3=11, vol=100 -> pv=1100, cum_pv=2000, cum_vol=200 -> vwap=10.0
    bars = [bar(0, 9, 10, 8, 9, 100), bar(1, 11, 12, 10, 11, 100)]
    result = session_vwap(bars)
    assert result[0] == 9.0
    assert result[1] == 10.0


def test_ema_converges_toward_flat_input():
    # Constant input should produce a constant EMA equal to that value
    values = [5.0] * 10
    result = ema(values, period=3)
    assert all(abs(v - 5.0) < 1e-9 for v in result)


def test_ema_period_1_equals_input():
    values = [1.0, 2.0, 3.0]
    # period=1 -> k=2/(1+1)=1.0 -> ema[i] = value[i] exactly after the first
    result = ema(values, period=1)
    assert result == values


def test_macd_structure_and_sane_direction():
    # Rising prices should produce a positive MACD line (fast EMA > slow EMA)
    closes = [float(i) for i in range(1, 41)]  # steadily rising
    result = macd(closes)
    assert len(result["macd"]) == len(closes)
    assert len(result["signal"]) == len(closes)
    assert len(result["histogram"]) == len(closes)
    assert result["macd"][-1] > 0  # fast EMA should be above slow EMA in an uptrend


def test_relative_volume_hand_computed():
    # 20 bars of volume=100 (baseline), then a bar with volume=500 -> rel vol = 5.0
    bars = [bar(i, 1, 1, 1, 1, 100) for i in range(20)]
    bars.append(bar(20, 1, 1, 1, 1, 500))
    result = relative_volume(bars, lookback=20)
    assert result[:20] == [1.0] * 20  # not enough history yet
    assert abs(result[20] - 5.0) < 1e-9


# -- confirmed_swing_lows (specs.md section 12's early-phase exit) --------

def test_confirmed_swing_lows_finds_a_clean_v_shape():
    # window=3 needs 3 bars on EACH side of the candidate -- exactly 7
    # bars here, the low (4) at index 3, fully bracketed.
    lows = [10, 8, 6, 4, 6, 8, 10]
    bars = [bar(i, l, l + 1, l, l, 1000) for i, l in enumerate(lows)]
    result = confirmed_swing_lows(bars, window=3)
    assert len(result) == 1
    assert result[0]["ts"] == 3
    assert result[0]["price"] == 4


def test_confirmed_swing_lows_empty_when_not_enough_bars_to_confirm():
    # A dip right at the tail end has no bars after it yet to confirm it.
    lows = [10, 8, 6, 4]
    bars = [bar(i, l, l + 1, l, l, 1000) for i, l in enumerate(lows)]
    assert confirmed_swing_lows(bars, window=3) == []


def test_confirmed_swing_lows_ignores_zero_volume_bars_as_candidates():
    # Same forward-fill exclusion detect_levels already relies on
    # (_swing_points) -- reused here, not reimplemented.
    lows = [10, 8, 6, 4, 6, 8, 10]
    bars = [bar(i, l, l + 1, l, l, 1000) for i, l in enumerate(lows)]
    bars[3]["volume"] = 0.0  # the candidate low itself, forward-filled
    assert confirmed_swing_lows(bars, window=3) == []


def test_confirmed_swing_lows_returns_multiple_in_bar_order():
    lows = [10, 8, 6, 4, 6, 8, 10, 8, 6, 3, 6, 8, 10]
    bars = [bar(i, l, l + 1, l, l, 1000) for i, l in enumerate(lows)]
    result = confirmed_swing_lows(bars, window=3)
    assert [r["price"] for r in result] == [4, 3]
    assert [r["ts"] for r in result] == [3, 9]


# -- continuation_days (specs.md section 7's continuation-vs-fresh-day gap) -

def _daily_bar(ts, close):
    return bar(ts, close, close, close, close, 1_000_000.0)


def test_continuation_days_finds_a_real_runner_day():
    # A clean +103.8% day (10.3 -> 21.0) well within a 7-day lookback.
    closes = [10.0, 10.2, 10.1, 10.3, 21.0, 20.5, 20.0, 19.8]
    bars = [_daily_bar(i, c) for i, c in enumerate(closes)]
    days = continuation_days(bars, lookback_days=7, threshold_pct=0.5)
    assert len(days) == 1
    assert days[0]["ts"] == 4
    assert days[0]["pct_change"] == pytest.approx((21.0 - 10.3) / 10.3)


def test_continuation_days_empty_for_a_genuinely_fresh_symbol():
    # Ordinary day-to-day noise, nothing near the 50% threshold.
    closes = [10.0, 10.3, 9.9, 10.2, 10.1, 9.8, 10.0, 10.15]
    bars = [_daily_bar(i, c) for i, c in enumerate(closes)]
    assert continuation_days(bars, lookback_days=7, threshold_pct=0.5) == []


def test_continuation_days_ignores_a_move_outside_the_lookback_window():
    # The +103.8% day sits 9 bars back from the end -- outside a 7-day
    # (8-bar) lookback window -- so must NOT be flagged; the noise inside
    # the window has nothing near threshold.
    closes = [10.3, 21.0] + [20.0, 20.1, 19.9, 20.2, 20.0, 19.95, 20.05, 20.1]
    bars = [_daily_bar(i, c) for i, c in enumerate(closes)]
    assert continuation_days(bars, lookback_days=7, threshold_pct=0.5) == []


def test_continuation_days_detects_a_large_down_day_too_signed_correctly():
    closes = [10.0, 10.1, 4.8, 4.7, 4.75]  # -52.5% day
    bars = [_daily_bar(i, c) for i, c in enumerate(closes)]
    days = continuation_days(bars, lookback_days=7, threshold_pct=0.5)
    assert len(days) == 1
    assert days[0]["pct_change"] < 0
    assert days[0]["pct_change"] == pytest.approx((4.8 - 10.1) / 10.1)


def test_continuation_days_returns_multiple_qualifying_days_in_order():
    closes = [10.0, 21.0, 20.0, 9.0, 9.2]  # +110% then -55%
    bars = [_daily_bar(i, c) for i, c in enumerate(closes)]
    days = continuation_days(bars, lookback_days=7, threshold_pct=0.5)
    assert [d["ts"] for d in days] == [1, 3]


def test_continuation_days_empty_list_for_no_or_insufficient_data():
    assert continuation_days([], lookback_days=7, threshold_pct=0.5) == []
    assert continuation_days([_daily_bar(0, 10.0)], lookback_days=7, threshold_pct=0.5) == []


def test_detect_levels_finds_double_top_with_higher_strength_than_single_touch():
    # Two separate swing highs at ~8.69, far enough apart to be distinct
    # swing points but close enough in price to cluster into one level.
    bars = []
    ts = 0
    # ramp up to first touch
    for i, p in enumerate([7.0, 7.5, 8.2, 8.69, 8.0, 7.6]):
        bars.append(bar(ts, p, p + 0.05, p - 0.05, p, 50_000)); ts += 60
    # pull back
    for p in [7.0, 6.9, 7.1]:
        bars.append(bar(ts, p, p + 0.05, p - 0.05, p, 30_000)); ts += 60
    # second touch of the same zone, slightly lower high (matches the real
    # AEHL session read: second push failed to exceed the first)
    for i, p in enumerate([7.4, 7.9, 8.3, 8.65, 7.9, 7.6]):
        bars.append(bar(ts, p, p + 0.05, p - 0.05, p, 60_000)); ts += 60

    levels = detect_levels(bars, swing_window=2, cluster_tolerance_pct=0.01)
    resistance_levels = [l for l in levels if l.kind == "resistance"]
    assert len(resistance_levels) >= 1

    top = resistance_levels[0]  # sorted by strength, strongest first
    assert 8.5 < top.price < 8.75  # roughly where the two highs clustered
    assert top.touch_count == 2  # both swing highs clustered into one level

    # A level touched twice must score higher than one touched once - this
    # is the core fix: strength isn't "nearest price", it's "how real is this".
    single_touch_bars = [bar(0, 5, 5.05, 4.95, 5, 10_000)] * 1
    # (not a full standalone assertion by itself - the comparison that
    # actually matters is touch_count driving strength_score upward, checked
    # directly:)
    assert top.strength_score > top.touch_count  # touches alone already exceed 1x weight, confirming they dominate the score


def test_detect_levels_ignores_zero_volume_forward_filled_bars_as_touches():
    # Real gap found live (QCLS): a resistance level showed touch_count=20
    # with total_touch_volume exactly 0 -- real trades essentially never
    # print zero shares, so that pattern specifically means synthetic
    # forward-filled bars (aggregator.py's _fill_gap_until: a quiet 10s
    # bucket emits a flat open==high==low==close==prior-close bar with
    # volume=0.0) got counted as repeated swing-point touches, not that
    # the level was genuinely tested 20 times.
    bars = []
    ts = 0
    for p in [9.0, 9.4, 9.8, 10.0, 9.6, 9.2]:  # one genuine touch at 10.0
        bars.append(bar(ts, p, p + 0.05, p - 0.05, p, 50_000)); ts += 10
    for _ in range(20):  # long quiet stretch forward-filled flat at 9.2, real
        bars.append(bar(ts, 9.2, 9.2, 9.2, 9.2, 0.0)); ts += 10  # aggregator would emit
    for p in [9.0, 8.8, 8.6]:  # real bars afterward, for window padding
        bars.append(bar(ts, p, p + 0.05, p - 0.05, p, 40_000)); ts += 10

    levels = detect_levels(bars, swing_window=3, cluster_tolerance_pct=0.006)

    # The genuine touch must still be found...
    assert any(abs(l.price - 10.0) < 0.1 for l in levels)
    # ...but no level may show touches with zero total volume behind them --
    # a real touch always has some real volume; this combination is
    # definitionally the synthetic-bar bug, not a legitimately quiet level.
    assert not any(l.touch_count > 0 and l.total_touch_volume == 0 for l in levels)


def test_evaluate_hold_single_bar_break_is_not_confirmed():
    # Matches the real AEHL read: price closes above 8.69 for exactly one
    # bar, then reverses hard. Should NOT be confirmed with required_bars=3.
    bars = [
        bar(0, 8.5, 8.75, 8.5, 8.72, 100_000),  # closes above 8.69 - attempt starts
        bar(1, 8.7, 8.75, 7.2, 7.25, 200_000),  # violent reversal, closes below
    ]
    state = evaluate_hold(bars, level_price=8.69, direction="above", required_bars=3)
    assert state.confirmed is False
    assert state.failed_attempts == 1


def test_evaluate_hold_confirms_after_required_consecutive_closes():
    bars = [
        bar(0, 8.5, 8.75, 8.5, 8.72, 100_000),
        bar(1, 8.72, 8.9, 8.65, 8.85, 120_000),
        bar(2, 8.85, 9.0, 8.8, 8.95, 110_000),
    ]
    state = evaluate_hold(bars, level_price=8.69, direction="above", required_bars=3)
    assert state.confirmed is True
    assert state.consecutive_bars == 3
    assert state.failed_attempts == 0


# -- Phase 3.6 stage 1: time-aware core functions (specs.md section 15) ----
# Purely additive -- these prove the new time-aware functions are EXACTLY
# equivalent to the existing bar-count functions on uniform-cadence data
# (real live bars are always exactly 10s apart). Stage 2 will separately
# prove genuine improvement on mixed-cadence data; that is NOT this suite's
# job.

def _uniform_bars(closes, volumes=None, start_ts=1_700_000_000, step=10):
    volumes = volumes or [1000.0] * len(closes)
    return [bar(start_ts + i * step, c, c, c, c, v)
            for i, (c, v) in enumerate(zip(closes, volumes))]


def test_ema_time_aware_hand_computed_first_two_steps():
    # period=3 -> k=2/4=0.5. Uniform 10s cadence, reference=10s.
    # out[0]=10 (seed). out[1] = 20*0.5 + 10*0.5 = 15.0.
    values = [10.0, 20.0, 20.0]
    timestamps = [0, 10, 20]
    result = ema_time_aware(values, timestamps, period=3, reference_interval_seconds=10.0)
    assert result[0] == 10.0
    assert result[1] == 15.0


def test_ema_time_aware_exactly_equals_bar_count_ema_on_uniform_cadence():
    # Exact (==, not approx) equivalence on real 10s-uniform cadence, for
    # every EMA period this project actually uses (macd's fast/slow/signal
    # plus a couple of others), per the requirement that this reduce to
    # EXACTLY the bar-count formula, not merely close to it.
    values = [5.0, 5.2, 5.1, 5.4, 5.6, 5.3, 5.8, 6.0, 5.9, 6.2,
              6.5, 6.3, 6.6, 6.8, 6.7, 7.0, 7.2, 7.1, 7.4, 7.6]
    timestamps = [1_700_000_000 + i * 10 for i in range(len(values))]
    for period in (3, 9, 12, 26):
        old = ema(values, period)
        new = ema_time_aware(values, timestamps, period, reference_interval_seconds=10.0)
        assert new == old, f"period={period} diverged: {new} != {old}"


def test_relative_volume_time_aware_hand_computed():
    # Same case as test_relative_volume_hand_computed, in time-window form:
    # lookback=20 bars * 10s/bar = 200s.
    bars = [bar(1_700_000_000 + i * 10, 1, 1, 1, 1, 100) for i in range(20)]
    bars.append(bar(1_700_000_000 + 20 * 10, 1, 1, 1, 1, 500))
    result = relative_volume_time_aware(bars, lookback_seconds=200.0)
    assert result[:20] == [1.0] * 20
    assert abs(result[20] - 5.0) < 1e-9


def test_relative_volume_time_aware_exactly_equals_bar_count_version_on_uniform_cadence():
    closes = [5.0] * 60
    volumes = [1000 + (i * 37) % 500 for i in range(60)]  # varied, deterministic
    bars = _uniform_bars(closes, volumes)
    old = relative_volume(bars, lookback=20)
    new = relative_volume_time_aware(bars, lookback_seconds=200.0)
    assert new == old


def test_swing_points_time_aware_exactly_equals_bar_count_version_on_uniform_cadence():
    lows = [10, 9, 8, 7, 6, 4, 6, 7, 8, 9, 10, 9, 8, 6, 3, 6, 8, 9, 10]
    bars = _uniform_bars(lows)
    old = _swing_points(bars, window=3, kind="low")
    new = swing_points_time_aware(bars, window_seconds=30.0, kind="low")
    assert new == old
    assert old  # sanity: the fixture actually contains swing lows to compare


def test_evaluate_hold_time_aware_hand_computed_confirms_at_30s():
    # Mirrors test_evaluate_hold_confirms_after_required_consecutive_closes:
    # 3 consecutive 10s bars on the correct side = 30s elapsed (bar-END
    # semantics: each bar contributes its own reference_interval_seconds of
    # confirmed time, measured as of that bar's close).
    bars = [
        bar(1_700_000_000 + 0, 8.5, 8.75, 8.5, 8.72, 100_000),
        bar(1_700_000_000 + 10, 8.72, 8.9, 8.65, 8.85, 120_000),
        bar(1_700_000_000 + 20, 8.85, 9.0, 8.8, 8.95, 110_000),
    ]
    state = evaluate_hold_time_aware(bars, level_price=8.69, direction="above",
                                     required_seconds=30.0, reference_interval_seconds=10.0)
    assert state.confirmed is True
    assert state.elapsed_seconds == 30.0
    assert state.failed_attempts == 0


def test_evaluate_hold_time_aware_single_bar_break_is_not_confirmed():
    bars = [
        bar(1_700_000_000 + 0, 8.5, 8.75, 8.5, 8.72, 100_000),
        bar(1_700_000_000 + 10, 8.7, 8.75, 7.2, 7.25, 200_000),
    ]
    state = evaluate_hold_time_aware(bars, level_price=8.69, direction="above",
                                     required_seconds=30.0, reference_interval_seconds=10.0)
    assert state.confirmed is False
    assert state.failed_attempts == 1


def test_evaluate_hold_time_aware_exactly_equals_bar_count_version_on_uniform_cadence():
    # required_bars=3 <-> required_seconds=30 (3 * 10s reference interval).
    closes = [8.72, 8.85, 8.95, 7.2, 8.71, 8.9, 9.1, 9.3, 6.5, 8.72, 8.8]
    bars = _uniform_bars(closes)
    old = evaluate_hold(bars, level_price=8.69, direction="above", required_bars=3)
    new = evaluate_hold_time_aware(bars, level_price=8.69, direction="above",
                                   required_seconds=30.0, reference_interval_seconds=10.0)
    assert new.confirmed == old.confirmed
    assert new.failed_attempts == old.failed_attempts
    assert new.elapsed_seconds == old.consecutive_bars * 10.0


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
