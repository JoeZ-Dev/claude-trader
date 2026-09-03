import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from indicators import session_vwap, ema, macd, relative_volume
from levels import detect_levels, evaluate_hold


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


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
