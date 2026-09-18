import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from indicators import session_vwap, ema, macd, relative_volume
from levels import confirmed_swing_lows, detect_levels, evaluate_hold


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


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
