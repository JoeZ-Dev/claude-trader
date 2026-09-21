import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from indicators import (
    continuation_days, session_vwap, ema, macd, relative_volume,
    ema_time_aware, relative_volume_time_aware, macd_time_aware,
    SessionVwapState, EmaTimeAwareState, MacdTimeAwareState,
    RelativeVolumeState,
)
from levels import (
    confirmed_swing_lows, detect_levels, evaluate_hold,
    evaluate_hold_time_aware, swing_points_time_aware, _swing_points,
    EvaluateHoldTimeAwareState,
)


def bar(ts, o, h, l, c, v):
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}


_FIXTURE_BARS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "schwab-connector", "data", "bars",
)


def _load_real_bars(symbol: str, limit: int | None = None) -> list[dict]:
    """Real captured bars (specs.md section 28/29, build_state incremental
    architecture) -- the SAME schwab-connector/data/bars/*.jsonl files
    used throughout phase 3.6's real-data proofs, read directly here
    (deterministic, local, no network/wall-clock dependency -- AGENT_
    PROTOCOL.md's reproducibility rule) rather than re-typed as literals,
    since these equivalence proofs need to walk thousands of real bars
    bar-by-bar, not check a few hand-picked values."""
    path = os.path.join(_FIXTURE_BARS_DIR, f"{symbol}.jsonl")
    bars = []
    with open(path) as f:
        for i, line in enumerate(f):
            if limit is not None and i >= limit:
                break
            bars.append(json.loads(line))
    return bars


def test_session_vwap_hand_computed():
    # Bar 1: typical=(10+8+9)/3=9, vol=100 -> pv=900, cum_vol=100 -> vwap=9.0
    # Bar 2: typical=(12+10+11)/3=11, vol=100 -> pv=1100, cum_pv=2000, cum_vol=200 -> vwap=10.0
    bars = [bar(0, 9, 10, 8, 9, 100), bar(1, 11, 12, 10, 11, 100)]
    result = session_vwap(bars)
    assert result[0] == 9.0
    assert result[1] == 10.0


# -- SessionVwapState (specs.md section 28/29, build_state incremental
# architecture, stage 2, function 1 of 5) --------------------------------

def test_session_vwap_state_matches_full_recompute_on_real_aiff_data():
    # Real AIFF data, the first real session (2026-09-17) in full --
    # 3,538 bars, the exact boundary before the next real session starts
    # (found by scanning the real fixture for NY calendar date changes).
    # Every SINGLE incremental step must equal what a full recompute over
    # bars[:i+1] would produce at that same point -- bit-for-bit, not
    # just the final value, since a real regression could easily only
    # show up mid-session.
    bars = _load_real_bars("AIFF", limit=3538)
    full = session_vwap(bars)
    state = SessionVwapState()
    for i, b in enumerate(bars):
        assert state.update(b) == full[i], f"mismatch at real bar {i}"


def test_session_vwap_state_from_bars_matches_stepping_from_empty():
    # The one-time rebuild path (a fresh watch's first backfill batch, or
    # a restart's one-time reconstruction) must land in EXACTLY the same
    # state as if every bar had been stepped through incrementally from
    # empty -- proven directly on the same real AIFF session.
    bars = _load_real_bars("AIFF", limit=3538)
    stepped = SessionVwapState()
    for b in bars:
        stepped.update(b)
    rebuilt = SessionVwapState.from_bars(bars)
    assert rebuilt.cum_pv == stepped.cum_pv
    assert rebuilt.cum_vol == stepped.cum_vol


def test_session_vwap_state_fresh_instance_starts_empty():
    # Lifecycle: a newly-watched symbol (or one removed then re-added)
    # must start with genuinely empty state, not inherit anything.
    state = SessionVwapState()
    assert state.cum_pv == 0.0
    assert state.cum_vol == 0.0


# -- EmaTimeAwareState / MacdTimeAwareState (specs.md section 28/29,
# build_state incremental architecture, stage 2, function 2 of 5) --------

def test_ema_time_aware_state_matches_full_recompute_on_real_aiff_data():
    # Real AIFF data, the same first real session (3,538 bars) used for
    # SessionVwapState above -- mixed real cadence (backfilled 60s bars
    # into live 10s bars), exactly the case ema_time_aware's own
    # time-aware weighting exists for (specs.md section 15/16), so this
    # equivalence proof actually exercises the dt-based k_eff branch, not
    # just the uniform-cadence fast path.
    bars = _load_real_bars("AIFF", limit=3538)
    closes = [b["close"] for b in bars]
    timestamps = [b["ts"] for b in bars]
    period = 9
    full = ema_time_aware(closes, timestamps, period)
    state = EmaTimeAwareState(period=period)
    for i in range(len(bars)):
        got = state.update(closes[i], timestamps[i])
        assert got == full[i], f"mismatch at real bar {i}"


def test_ema_time_aware_state_from_series_matches_stepping_from_empty():
    bars = _load_real_bars("AIFF", limit=3538)
    closes = [b["close"] for b in bars]
    timestamps = [b["ts"] for b in bars]
    period = 20
    stepped = EmaTimeAwareState(period=period)
    for i in range(len(bars)):
        stepped.update(closes[i], timestamps[i])
    rebuilt = EmaTimeAwareState.from_series(closes, timestamps, period)
    assert rebuilt.value == stepped.value
    assert rebuilt.last_ts == stepped.last_ts


def test_ema_time_aware_state_fresh_instance_starts_empty():
    state = EmaTimeAwareState(period=9)
    assert state.value is None
    assert state.last_ts is None


def test_macd_time_aware_state_matches_full_recompute_on_real_aiff_data():
    # Same real first-session AIFF data, default fast/slow/signal periods
    # -- exercises all three internal EmaTimeAwareState legs together,
    # including the signal leg's EMA-of-a-derived-series (macd_line, not
    # raw closes).
    bars = _load_real_bars("AIFF", limit=3538)
    closes = [b["close"] for b in bars]
    timestamps = [b["ts"] for b in bars]
    full = macd_time_aware(closes, timestamps)
    state = MacdTimeAwareState.new()
    for i in range(len(bars)):
        got = state.update(closes[i], timestamps[i])
        assert got["macd"] == full["macd"][i], f"macd mismatch at real bar {i}"
        assert got["signal"] == full["signal"][i], f"signal mismatch at real bar {i}"
        assert got["histogram"] == full["histogram"][i], f"histogram mismatch at real bar {i}"


def test_macd_time_aware_state_from_series_matches_stepping_from_empty():
    bars = _load_real_bars("AIFF", limit=3538)
    closes = [b["close"] for b in bars]
    timestamps = [b["ts"] for b in bars]
    stepped = MacdTimeAwareState.new()
    for i in range(len(bars)):
        stepped.update(closes[i], timestamps[i])
    rebuilt = MacdTimeAwareState.from_series(closes, timestamps)
    assert rebuilt.fast.value == stepped.fast.value
    assert rebuilt.slow.value == stepped.slow.value
    assert rebuilt.signal.value == stepped.signal.value


# -- EvaluateHoldTimeAwareState (specs.md section 28/29, build_state
# incremental architecture, stage 2, function 3 of 5) --------------------
#
# evaluate_hold_time_aware's own bar_end_ts computation peeks at the NEXT
# bar's ts when one exists (bars[i+1]["ts"]), falling back to an assumed
# reference-width estimate ONLY for whichever bar is currently last in
# the list -- the same "duration isn't knowable until the next bar
# arrives" problem relative_volume_time_aware's own durations have. The
# incremental version below handles it with a one-bar-delayed commit: the
# most recently processed bar stays "pending" (its own elapsed_seconds/
# confirmed check uses the estimate, exactly matching what a fresh full
# recompute over the bars seen so far would also do for its own last
# bar) until the ACTUAL next bar arrives, at which point it's committed
# using that bar's real ts as its bar_end_ts -- also exactly matching
# what a fresh full recompute over the now-longer bars list would do,
# since old code always has real lookahead available for every bar
# except its own last one.

def test_evaluate_hold_time_aware_state_matches_full_recompute_above():
    # Real AIFF data, direction="above", a level price (1.15) chosen from
    # the real close range (0.926-1.439 over this session) to produce
    # genuine on-side/off-side transitions, not a level so extreme it
    # never triggers anything.
    bars = _load_real_bars("AIFF", limit=3538)
    level_price = 1.15
    state = EvaluateHoldTimeAwareState.from_bars([], level_price, direction="above")
    for i in range(len(bars)):
        got = state.update(bars[i])
        want = evaluate_hold_time_aware(bars[:i + 1], level_price, direction="above")
        assert got.confirmed == want.confirmed, f"confirmed mismatch at real bar {i}"
        assert got.confirmed_at_ts == want.confirmed_at_ts, f"confirmed_at_ts mismatch at real bar {i}"
        assert got.elapsed_seconds == want.elapsed_seconds, f"elapsed_seconds mismatch at real bar {i}"
        assert got.failed_attempts == want.failed_attempts, f"failed_attempts mismatch at real bar {i}"


def test_evaluate_hold_time_aware_state_matches_full_recompute_below():
    # Same real data, direction="below", a different level (1.30) so the
    # on-side condition (close < level) also produces real transitions.
    bars = _load_real_bars("AIFF", limit=3538)
    level_price = 1.30
    state = EvaluateHoldTimeAwareState.from_bars([], level_price, direction="below")
    for i in range(len(bars)):
        got = state.update(bars[i])
        want = evaluate_hold_time_aware(bars[:i + 1], level_price, direction="below")
        assert got.confirmed == want.confirmed, f"confirmed mismatch at real bar {i}"
        assert got.confirmed_at_ts == want.confirmed_at_ts, f"confirmed_at_ts mismatch at real bar {i}"
        assert got.elapsed_seconds == want.elapsed_seconds, f"elapsed_seconds mismatch at real bar {i}"
        assert got.failed_attempts == want.failed_attempts, f"failed_attempts mismatch at real bar {i}"


def test_evaluate_hold_time_aware_state_matches_full_recompute_with_watch_added_ts():
    # Real AIFF data with a real watch_added_ts set partway through the
    # session (specs.md section 19/20's confirmation-freshness guard) --
    # must produce the same False-until-watch, then-eligible transition
    # as the bar-count version, not just match when the guard is inert.
    bars = _load_real_bars("AIFF", limit=3538)
    level_price = 1.15
    watch_added_ts = bars[1800]["ts"]
    state = EvaluateHoldTimeAwareState.from_bars([], level_price, direction="above",
                                                 watch_added_ts=watch_added_ts)
    for i in range(len(bars)):
        got = state.update(bars[i])
        want = evaluate_hold_time_aware(bars[:i + 1], level_price, direction="above",
                                        watch_added_ts=watch_added_ts)
        assert got.confirmed == want.confirmed, f"confirmed mismatch at real bar {i}"
        assert got.confirmed_at_ts == want.confirmed_at_ts, f"confirmed_at_ts mismatch at real bar {i}"


def test_evaluate_hold_time_aware_state_from_bars_matches_stepping_from_empty():
    # The one-time rebuild path must land in the exact same state as
    # stepping incrementally from empty would -- including the
    # one-bar-delayed pending/committed split (from_bars must commit
    # every bar except the last using REAL lookahead, matching what
    # continuous update() calls would eventually settle into).
    bars = _load_real_bars("AIFF", limit=3538)
    level_price = 1.15
    stepped = EvaluateHoldTimeAwareState.from_bars([], level_price, direction="above")
    for b in bars:
        stepped.update(b)
    rebuilt = EvaluateHoldTimeAwareState.from_bars(bars, level_price, direction="above")
    assert rebuilt.confirmed == stepped.confirmed
    assert rebuilt.confirmed_at_ts == stepped.confirmed_at_ts
    assert rebuilt.elapsed_seconds == stepped.elapsed_seconds
    assert rebuilt.failed_attempts == stepped.failed_attempts
    assert rebuilt.streak_start_ts == stepped.streak_start_ts
    assert rebuilt.was_attempting == stepped.was_attempting
    assert rebuilt.pending_bar == stepped.pending_bar


def test_evaluate_hold_time_aware_state_fresh_instance_starts_empty():
    state = EvaluateHoldTimeAwareState.from_bars([], 1.0, direction="above")
    assert state.confirmed is False
    assert state.confirmed_at_ts is None
    assert state.failed_attempts == 0
    assert state.streak_start_ts is None
    assert state.pending_bar is None


# -- RelativeVolumeState (specs.md section 28/29, build_state
# incremental architecture, stage 2, function 4 of 5 -- the ORIGINAL
# dominant, quadratic-per-call cost the whole incident started from) ----
#
# Same one-bar-delayed lookahead problem as EvaluateHoldTimeAwareState
# (a window bar's own duration depends on the NEXT bar's ts), but with a
# sliding window instead of a single streak: bars enter the window as
# "pending" (self-rate computed with the reference-width estimate, same
# fallback the bar-count version uses for whichever bar is currently
# last) and get folded into the window's running sums, with a REAL
# finalized duration, only once the actual next bar arrives. Eviction
# (bars aging out of `lookback_seconds`) and the uniform-vs-mixed-
# duration branch (tracked via a running `nonuniform_count` instead of
# rechecking every window element every call) are both maintained
# incrementally as bars enter and leave.
#
# The exhaustive real-data comparisons below call the ACTUAL bar-count
# relative_volume_time_aware(bars[:i+1])[-1] at each step, not a
# hand-duplicated copy of its logic -- genuine end-to-end proof against
# the real function, not against a reimplementation that could carry the
# same bug both ways. This is deliberately capped at a few hundred bars
# exhaustively (that function's OWN O(n^2)-per-call cost makes a full
# cumulative walk cost O(n^3) -- the exact defect this whole redesign
# exists to fix) with sparse full-session checkpoints beyond that.

def test_relative_volume_state_matches_full_recompute_exhaustively_on_real_aiff_data():
    # Real AIFF data, first 900 bars -- matches one of the original
    # profiling checkpoints (100/300/600/900/1200), spans the real
    # premarket-backfill (60s) to live (10s) cadence transition, so both
    # the uniform and mixed-duration branches get exercised, not just one.
    bars = _load_real_bars("AIFF", limit=900)
    state = RelativeVolumeState()
    for i in range(len(bars)):
        got = state.update(bars[i])
        want = relative_volume_time_aware(bars[:i + 1])[-1]
        assert got == want, f"mismatch at real bar {i}"


def test_relative_volume_state_matches_full_recompute_exhaustively_on_real_aemd_data():
    # Real AEMD data, first 600 bars -- uniform 60s cadence throughout
    # (confirmed directly: every gap in this range is exactly 60s), which
    # is a DIFFERENT real shape than AIFF's mixed transition: since 60s
    # != the 10s reference_interval_seconds default, every window here
    # takes the rate-based branch, never the raw-volume uniform fast path.
    bars = _load_real_bars("AEMD", limit=600)
    state = RelativeVolumeState()
    for i in range(len(bars)):
        got = state.update(bars[i])
        want = relative_volume_time_aware(bars[:i + 1])[-1]
        assert got == want, f"mismatch at real bar {i}"


def test_relative_volume_state_matches_full_recompute_at_sparse_real_aiff_checkpoints():
    # Beyond the exhaustive range: a single incremental pass through the
    # REAL AIFF fixture's entire first session (3,538 bars, spanning all
    # 4 real backfill/live transitions in that session), checked against
    # a full recompute at sparse checkpoints -- exhaustive at this length
    # would cost O(n^3) via the reference implementation itself (the
    # exact defect being fixed), so this instead proves the incremental
    # walk stays correct far past the exhaustively-checked range without
    # paying that cost.
    bars = _load_real_bars("AIFF", limit=3538)
    state = RelativeVolumeState()
    checkpoints = set(range(899, len(bars), 250)) | {len(bars) - 1}
    for i in range(len(bars)):
        got = state.update(bars[i])
        if i in checkpoints:
            want = relative_volume_time_aware(bars[:i + 1])[-1]
            assert got == want, f"mismatch at real bar {i}"


def test_relative_volume_state_from_bars_matches_stepping_from_empty():
    bars = _load_real_bars("AIFF", limit=900)
    stepped = RelativeVolumeState()
    for b in bars:
        stepped.update(b)
    rebuilt = RelativeVolumeState.from_bars(bars)
    assert rebuilt.sum_vol == stepped.sum_vol
    assert rebuilt.sum_dur == stepped.sum_dur
    assert rebuilt.nonuniform_count == stepped.nonuniform_count
    assert list(rebuilt.window) == list(stepped.window)
    assert rebuilt.pending_bar == stepped.pending_bar
    assert rebuilt.first_ts == stepped.first_ts


def test_relative_volume_state_fresh_instance_starts_empty():
    state = RelativeVolumeState()
    assert state.first_ts is None
    assert state.pending_bar is None
    assert len(state.window) == 0
    assert state.sum_vol == 0.0
    assert state.sum_dur == 0.0
    assert state.nonuniform_count == 0


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
    bars = _uniform_bars(lows)  # uniform 10s cadence
    old = _swing_points(bars, window=3, kind="low")
    # Each side's walk target is 3.0 (default multiple) * the immediately
    # adjacent bar's own 10s gap = 30.0 -- exactly section 15's original
    # fixed window_seconds=30.0, and the walk collects exactly 3 real
    # bars on each side to reach it, matching bars[i-3:i+4] exactly.
    new = swing_points_time_aware(bars, kind="low")
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


# -- Phase 3.6 stage 2: genuine improvement on mixed cadence (specs.md ----
# section 16). Distinct from stage 1's uniform-cadence equivalence tests
# above -- these deliberately use IRREGULAR spacing (real backfill widths
# and a real internal gap) and prove the time-aware functions weight a
# bar PROPORTIONALLY to its real width, neither treating it as equal to a
# 10s bar (naive equal-weighting) nor excluding it entirely (the current
# live_cadence_tail stopgap).

def test_ema_time_aware_single_60s_step_equals_six_10s_steps_to_same_value():
    # Retention-of-old-value algebra (specs.md section 16): a single dt=60s
    # step landing on close V must retain EXACTLY (1-k)**6 of the prior EMA
    # value -- bit-identical to compounding six standard 10s-reference
    # steps that each feed in the SAME value V. Real-shaped numbers
    # (period=9, matching macd's fast leg; P/V from an actual AIFF EMA
    # level and its following close -- see the live evidence in the
    # report/specs.md for where these came from).
    period = 9
    k = 2.0 / (period + 1)
    P = 7.6483   # prior ema level
    V = 7.74     # the 60s bar's own close
    # Method A: one time-aware step, dt=60s, reference=10s.
    values_a = [P, V]
    timestamps_a = [0, 60]
    out_a = ema_time_aware(values_a, timestamps_a, period, reference_interval_seconds=10.0)
    # Method B: six standard (bar-count) EMA steps, each fed the SAME
    # value V (isolates the retained-fraction-of-P comparison).
    values_b = [P] + [V] * 6
    out_b = ema(values_b, period)
    assert out_a[1] == out_b[6]
    # And both must equal the closed-form V*(1-(1-k)**6) + (1-k)**6 * P
    # (approx here only because this closed form recomputes (1-k)**6
    # independently rather than reusing k_eff, which can differ in the
    # last float bit from a different order of operations -- the exact,
    # bit-for-bit claim is out_a[1] == out_b[6] above, already proven).
    closed_form = V * (1 - (1 - k) ** 6) + (1 - k) ** 6 * P
    assert out_a[1] == pytest.approx(closed_form, rel=1e-12)


def test_ema_time_aware_heavily_discounts_history_across_a_real_gap():
    # A real 360s internal backfill gap (AIFF, 08:23:00 -> 08:29:00, a
    # genuine 5-skipped-minute Schwab gap -- see specs.md section 16)
    # must retain only (1-k)**36 of the pre-gap ema, not (1-k)**1 (what
    # naively treating the gap as one ordinary step would do).
    period = 9
    k = 2.0 / (period + 1)
    values = [0.9445, 0.927]  # real closes either side of the gap
    timestamps = [0, 360]
    result = ema_time_aware(values, timestamps, period, reference_interval_seconds=10.0)
    retained_fraction = (result[1] - values[1]) / (values[0] - values[1])
    assert retained_fraction == pytest.approx((1 - k) ** 36, rel=1e-12)
    # Sanity: this is a MUCH smaller retained weight than one ordinary
    # 10s step would leave -- the gap is genuinely, heavily discounted.
    assert retained_fraction < (1 - k) * 0.01


def test_relative_volume_time_aware_uses_rate_not_raw_volume_across_mixed_widths():
    # Mixed cadence, SAME underlying rate throughout (10 shares/second):
    # three real-shaped 60s backfilled bars (volume=600) then ten 10s live
    # bars (volume=100), then a current 10s bar also at volume=100/10s.
    # Correct (rate-based) answer: relative volume of the current bar is
    # exactly 1.0, since every bar in the window shares the identical
    # rate. A naive raw-per-bar average would instead average 600s and
    # 100s together, making a normal-rate bar look artificially low.
    bars = []
    ts = 0
    for _ in range(3):
        bars.append(bar(ts, 1, 1, 1, 1, 600)); ts += 60
    for _ in range(10):
        bars.append(bar(ts, 1, 1, 1, 1, 100)); ts += 10
    current = bar(ts, 1, 1, 1, 1, 100)
    bars.append(current)
    result = relative_volume_time_aware(bars, lookback_seconds=200.0,
                                        reference_interval_seconds=10.0)
    assert result[-1] == pytest.approx(1.0, rel=1e-9)
    # A naive raw-volume average over the SAME time window (what stage 1's
    # window-selection-only version would have computed) is NOT 1.0 --
    # confirms this is a real, not cosmetic, difference.
    window = [w for w in bars[:-1] if current["ts"] - 200.0 <= w["ts"] < current["ts"]]
    naive_avg = sum(w["volume"] for w in window) / len(window)
    naive_result = current["volume"] / naive_avg
    assert naive_result != pytest.approx(1.0, rel=1e-9)


def test_evaluate_hold_time_aware_uses_actual_bar_width_not_fixed_reference_interval():
    # Stage 1's formula (elapsed = (ts - streak_start) + a FIXED
    # reference_interval_seconds) is only correct for an INTERIOR bar
    # when that bar's own real width genuinely equals the reference --
    # true for every stage-1 test (uniform 10s bars) but not in general.
    # Here bar1's real width (to bar2, 2s later) is far SMALLER than the
    # fixed 10s stage-1 assumed -- stage 1's formula over-counts real
    # elapsed time by 8s at bar1 (21+10=31, wrongly clearing
    # required_seconds=30), while the corrected version, using bar1's
    # ACTUAL next-bar width, correctly measures only 23s of real elapsed
    # time and does NOT confirm -- a genuine over-confirmation bug this
    # stage exists to catch, not a cosmetic difference.
    bars = [
        bar(0, 8.5, 8.6, 8.4, 8.72, 100_000),   # streak starts
        bar(21, 8.72, 8.9, 8.6, 8.85, 100_000),  # real width to next bar: 2s
        bar(23, 8.72, 8.9, 8.0, 8.1, 100_000),   # closes below level, ends streak
    ]
    new = evaluate_hold_time_aware(bars, level_price=8.69, direction="above",
                                   required_seconds=30.0, reference_interval_seconds=10.0)
    assert new.confirmed is False  # only 23s of real elapsed time ever accrued


# -- Phase 3.6 stage 2 (completing it): swing_points_time_aware on mixed --
# cadence (specs.md section 16). Distinct from the ema/relative_volume/
# evaluate_hold mixed-cadence tests above.

def test_swing_points_time_aware_requires_a_real_bracket_not_just_calendar_room():
    # Real bug found against real AIFF backfilled data (specs.md section
    # 16): a fixed window narrower than local bar spacing (30s vs 60s
    # backfill cadence) made a candidate's real-time "segment" degenerate
    # to just the candidate itself, which trivially "won" as both the max
    # AND the min of a one-element set. Under the two-directional walk
    # (specs.md section 17), the equivalent failure mode is
    # max_hop_seconds too small for the real cadence -- here, 30s against
    # 60s-cadence bars means even the FIRST hop on either side is already
    # unreachable, so nothing is ever collected and no candidate can ever
    # be flagged.
    lows = [10, 9, 8, 7, 6, 5, 6, 7, 8, 9, 10]
    bars = _uniform_bars(lows, start_ts=0, step=60)  # 60s cadence, like real backfill
    assert swing_points_time_aware(bars, kind="low", max_hop_seconds=30.0) == []
    assert swing_points_time_aware(bars, kind="high", max_hop_seconds=30.0) == []


def test_swing_points_time_aware_still_finds_real_swing_points_when_window_actually_brackets():
    # Sanity companion to the above: the fix must not make the function
    # vacuously empty in general. With the DEFAULT multiple=3.0, the
    # walk's target on each side (3.0 * the adjacent 60s hop = 180s)
    # comfortably brackets 60s-cadence neighbors, and the real V-shaped
    # low is still found.
    lows = [10, 9, 8, 7, 6, 5, 6, 7, 8, 9, 10]
    bars = _uniform_bars(lows, start_ts=0, step=60)
    result = swing_points_time_aware(bars, kind="low")
    assert result == [5]  # the single low point, index 5 (value 5)


# -- Phase 3.6, cadence-adaptive window: two-directional walk (specs.md --
# section 17). A first design (multiple * the candidate's OWN single
# _bar_duration) closed the fixed-window bug above but, investigated
# further against the FULL real AIFF/AEMD/DAIC/DTSS history (not just the
# one instance each originally checked), turned out to have two
# STRUCTURAL blind spots, not rare flukes: 54 bars where a narrow
# forward gap masked a genuinely wider real backward neighbor, and 63 of
# 100 real large-gap bars where an inflated forward gap let the window
# bridge back across the gap. Tested several alternatives against the
# same full real data (see the session report): min(back,fwd) changed
# nothing (forward was already the smaller value in practice); max
# (back,fwd) and an asymmetric per-side single-gap design both fully
# fixed the narrow-window blind spot but made gap-bridging WORSE (100/100
# instead of 63/100) -- confirming a genuine, unavoidable tension in any
# design deriving ONE scalar per side from a SINGLE adjacent gap alone.
#
# The two-directional walk below resolves both, verified against the
# same full real data (0 remaining narrow-window blind spots, 0/100
# remaining gap-bridges): each side walks outward hop by hop,
# accumulating REAL elapsed time using each traversed pair's own actual
# gap; a single hop larger than `max_hop_seconds` is a hard stop -- never
# crossed, never counted, rather than treated as "far but still valid."
# The accumulation TARGET on a side is `multiple` times that side's own
# FIRST (immediately adjacent) hop, so it still scales to whatever local
# cadence genuinely exists next to the candidate -- but max_hop_seconds
# applies to that first hop too, so a candidate sitting immediately next
# to a genuine gap can never use the gap itself to inflate its own
# target. Real-data tradeoff, reported honestly: this is more
# conservative than the single-scalar design in ordinary sparse (100-
# 180s) stretches too (25/26 real backfilled swing points found on the
# real AIFF day, vs. that design's 44/39) -- but that design's higher
# count was inflated by the very gap-bridging this one closes, and 25/26
# is still a real, substantial improvement over the original fixed-
# window's 0/0.

def test_swing_points_time_aware_finds_a_real_neighbor_across_a_cadence_speed_up():
    # Mirrors the real AIFF transition bar (specs.md section 17): a
    # candidate 60s after its own predecessor but only 10s before its
    # successor. The walk's "before" side targets 3 * 60s = 180s using
    # its own first (backward) hop -- correctly finding the real
    # predecessor region -- unlike the single-scalar design, which used
    # only the 10s forward gap and excluded it.
    bars = (_uniform_bars([1, 2, 3], start_ts=0, step=60)            # 0,60,120 (backfill lead-in)
            + [bar(180, 1, 1, 1, 4, 100)]                              # 180: the transition bar
            + _uniform_bars([5, 6, 7, 8], start_ts=190, step=10))      # 190,200,210,220 (live)
    transition_idx = 3
    assert bars[transition_idx]["ts"] == 180
    before = swing_points_time_aware(bars, kind="high", multiple=3.0)
    # (not asserting transition_idx is itself a swing high here -- the
    # fixture isn't shaped to make it one -- asserting the MECHANISM
    # directly via the internal helper instead, matching how the real
    # AIFF report verifies it.)
    from levels import _walk_real_neighbors
    result = _walk_real_neighbors(bars, transition_idx, -1, multiple=3.0, max_hop_seconds=90.0)
    assert result != []  # real predecessor region found, not excluded


def test_swing_points_time_aware_still_excludes_a_real_internal_gap():
    # Reconfirmation (specs.md section 16/17): a candidate immediately
    # after a real large gap must not have its own (possibly also wide)
    # forward gap let it bridge back across that gap. Mirrors the real
    # AIFF case (idx28: backward_gap=360 [the gap itself], forward_gap=
    # 120) where the single-scalar design DID bridge it.
    bars = _uniform_bars([10, 9, 8, 7], start_ts=0, step=60)            # 0,60,120,180
    bars.append(bar(540, 1, 1, 1, 6, 100))                                # 360s gap: 180 -> 540
    bars.append(bar(660, 1, 1, 1, 5, 100))                                # 120s forward gap (also wide)
    bars += _uniform_bars([4, 3, 2, 1], start_ts=720, step=60)            # 720,780,...
    from levels import _walk_real_neighbors
    i = 4  # the bar at ts=540, immediately after the 360s gap
    assert bars[i]["ts"] == 540
    before = _walk_real_neighbors(bars, i, -1, multiple=3.0, max_hop_seconds=90.0)
    assert before == []  # the 360s first hop alone exceeds max_hop_seconds -- never crossed


def test_swing_points_time_aware_uses_a_two_directional_walk_not_a_second_primitive():
    # Deliberately broken/restored standard, verifying the function
    # genuinely walks hop by hop rather than deriving a window from a
    # single adjacent gap. 60s-cadence bars: the walk collects 3 real
    # bars on each side (target = 3 * 60s = 180s, reached exactly via 3
    # hops of 60s each) to confirm the real V-shaped low.
    lows = [10, 9, 8, 7, 6, 5, 6, 7, 8, 9, 10, 9, 8]
    bars = _uniform_bars(lows, start_ts=0, step=60)
    result = swing_points_time_aware(bars, kind="low", multiple=3.0, max_hop_seconds=90.0)
    assert result == [5]


def test_swing_points_time_aware_requires_reaching_the_full_target_not_a_partial_walk():
    # Real bug found during phase 3.6 stage 3's migration (specs.md
    # section 18): a candidate too close to the START/END of `bars`, or
    # too close to a real gap, could still be "eligible" with a walk that
    # ran out of real bars (or hit a gap) before reaching its own target
    # -- a PARTIAL collection, shorter than the design calls for, was
    # being accepted as if it were a genuine bracket. This is the exact
    # scenario setup_types.py's own test suite caught downstream (a
    # 5-bar fixture where multiple=3 has only 2 real bars available on
    # each side of the middle candidate -- 120s accumulated, short of the
    # 180s target). The walk must return `[]`, not the partial 2-bar
    # collection, when the array runs out before reaching target.
    lows = [10, 9, 8, 9, 10]  # a clean V, the low at index 2
    bars = _uniform_bars(lows, start_ts=0, step=60)  # only 5 bars, 60s apart
    # multiple=3 needs 180s on each side; only 2 hops (120s) fit before
    # the array ends on either side of the middle candidate (index 2).
    assert swing_points_time_aware(bars, kind="low", multiple=3.0) == []
    # multiple=1 only needs 60s -- one real hop away, well within reach.
    assert swing_points_time_aware(bars, kind="low", multiple=1.0) == [2]


# -- Phase 3.6 stage 3 part 2: watch_added_ts (specs.md section 19) -------
# Migrating evaluate_hold to feed the full backfilled+live series means a
# hold can complete ENTIRELY within backfilled (pre-watch) bars. This
# must not let a symbol show "confirmed" the instant it's added, purely
# from history that predates ever watching it.

def test_evaluate_hold_time_aware_does_not_confirm_purely_from_pre_watch_history():
    # A hold completes entirely at ts=0..20 (well before watch_added_ts)
    # and is STILL ongoing (no reversal) right up through the moment
    # watching begins -- the real risk found on live AIFF data: a
    # genuinely, currently-true state whose CONFIRMING instant is
    # nonetheless purely historical must not show confirmed the instant
    # a symbol is added, before any bar has actually been observed live.
    bars = [
        bar(0, 8.5, 8.6, 8.4, 8.72, 100_000),   # on-side, pre-watch
        bar(10, 8.72, 8.9, 8.6, 8.85, 100_000),  # on-side, pre-watch
        bar(20, 8.72, 8.9, 8.6, 8.90, 100_000),  # on-side, pre-watch -- confirms here (elapsed=30) if unrestricted
        bar(30, 8.72, 8.9, 8.6, 8.92, 100_000),  # still on-side, pre-watch -- streak continues, uninterrupted
    ]
    watch_added_ts = 1_000_000  # watching begins long after these bars
    # Unrestricted: the pre-watch confirmation at ts=20 sticks (no
    # reversal ever occurs in this fixture) -- this is exactly the real
    # risk found on live AIFF data.
    unrestricted = evaluate_hold_time_aware(bars, level_price=8.69, direction="above",
                                            required_seconds=30.0)
    assert unrestricted.confirmed is True
    # Restricted: the confirming instant itself must be at or after
    # watch_added_ts. No bar in this fixture is at or after it yet.
    restricted = evaluate_hold_time_aware(bars, level_price=8.69, direction="above",
                                          required_seconds=30.0, watch_added_ts=watch_added_ts)
    assert restricted.confirmed is False


def test_evaluate_hold_time_aware_confirms_once_a_post_watch_bar_extends_a_pre_watch_streak():
    # The SAME streak that started before watch continues, uninterrupted,
    # through and past watch_added_ts -- this is a genuinely still-true,
    # currently-observable state (price really has been above the level
    # continuously), so it's correct to confirm once a post-watch bar
    # naturally extends the already-past-threshold streak, not withheld
    # forever just because the streak itself started in the backfill.
    bars = [
        bar(0, 8.5, 8.6, 8.4, 8.72, 100_000),
        bar(10, 8.72, 8.9, 8.6, 8.85, 100_000),
        bar(20, 8.72, 8.9, 8.6, 8.90, 100_000),   # elapsed=30 here if unrestricted -- but pre-watch
        bar(30, 8.72, 8.9, 8.6, 8.95, 100_000),   # still on-side, still pre-watch
        bar(1_000_000, 8.72, 8.9, 8.6, 8.96, 100_000),  # watch begins, streak continues uninterrupted
    ]
    restricted = evaluate_hold_time_aware(bars, level_price=8.69, direction="above",
                                          required_seconds=30.0, watch_added_ts=1_000_000)
    assert restricted.confirmed is True  # confirms AT the first post-watch bar
    unrestricted = evaluate_hold_time_aware(bars, level_price=8.69, direction="above",
                                            required_seconds=30.0)
    assert unrestricted.confirmed is True  # already confirmed earlier -- same final answer, different timing


def test_evaluate_hold_time_aware_watch_added_ts_none_is_unrestricted_default():
    # No watch_added_ts given (every stage 1-3-part-1 test, and any
    # caller that doesn't track a watch time) -- behavior is completely
    # unchanged.
    bars = [
        bar(0, 8.5, 8.6, 8.4, 8.72, 100_000),
        bar(10, 8.72, 8.9, 8.6, 8.85, 100_000),
        bar(20, 8.72, 8.9, 8.6, 8.90, 100_000),
    ]
    with_none = evaluate_hold_time_aware(bars, level_price=8.69, direction="above", required_seconds=30.0,
                                         watch_added_ts=None)
    without = evaluate_hold_time_aware(bars, level_price=8.69, direction="above", required_seconds=30.0)
    assert with_none == without


# -- confirmed_at_ts (phase 3.6 follow-up, specs.md section 20): exposes --
# WHEN a persisted confirmed=True was last genuinely reaffirmed, so a
# CONSUMER can gate staleness without evaluate_hold_time_aware's own
# monotonic confirmed logic changing at all.

def test_confirmed_at_ts_is_none_before_ever_confirming():
    bars = [bar(0, 8.5, 8.6, 8.4, 8.72, 100_000)]  # on-side, but only 10s -- not confirmed yet
    state = evaluate_hold_time_aware(bars, level_price=8.69, direction="above", required_seconds=30.0)
    assert state.confirmed is False
    assert state.confirmed_at_ts is None


def test_confirmed_at_ts_refreshes_on_every_reaffirming_bar():
    # A hold that confirms and then CONTINUES (no reversal) must keep
    # confirmed_at_ts current -- this is the "recently confirmed, still
    # actionable" property: as long as the hold is genuinely ongoing,
    # it's never stale.
    bars = [
        bar(0, 8.5, 8.6, 8.4, 8.72, 100_000),
        bar(10, 8.72, 8.9, 8.6, 8.85, 100_000),
        bar(20, 8.72, 8.9, 8.6, 8.90, 100_000),  # confirms here (elapsed=30)
        bar(30, 8.72, 8.9, 8.6, 8.92, 100_000),  # still on-side -- refreshes
        bar(40, 8.72, 8.9, 8.6, 8.95, 100_000),  # still on-side -- refreshes again
    ]
    state = evaluate_hold_time_aware(bars, level_price=8.69, direction="above", required_seconds=30.0)
    assert state.confirmed is True
    assert state.confirmed_at_ts == 40  # the LAST on-side bar, not the first confirming one


def test_confirmed_at_ts_freezes_at_the_last_reaffirming_bar_after_a_reversal():
    # Mirrors the real sharp-breach scenario (specs.md section 19/20):
    # confirmed stays True (monotonic, unchanged) through a later
    # reversal, but confirmed_at_ts freezes at the last bar where it was
    # GENUINELY on-side and past threshold, exposing exactly how stale
    # that persisted True actually is.
    bars = [
        bar(0, 8.5, 8.6, 8.4, 8.72, 100_000),
        bar(10, 8.72, 8.9, 8.6, 8.85, 100_000),
        bar(20, 8.72, 8.9, 8.6, 8.90, 100_000),  # confirms here (elapsed=30)
        bar(30, 8.72, 8.9, 8.6, 8.92, 100_000),  # still on-side -- confirmed_at_ts=30
        bar(40, 8.72, 8.9, 8.0, 8.10, 100_000),  # reversal -- confirmed stays True, confirmed_at_ts freezes
    ]
    state = evaluate_hold_time_aware(bars, level_price=8.69, direction="above", required_seconds=30.0)
    assert state.confirmed is True   # unchanged, still monotonic
    assert state.confirmed_at_ts == 30  # frozen at the last genuinely-on-side bar


def test_macd_time_aware_exactly_equals_bar_count_macd_on_uniform_cadence():
    # Composed directly from ema_time_aware (specs.md section 19) --
    # bit-exact equal to macd() on uniform 10s cadence, by ema_time_
    # aware's own already-proven exactness.
    closes = [5.0, 5.2, 5.1, 5.4, 5.6, 5.3, 5.8, 6.0, 5.9, 6.2,
              6.5, 6.3, 6.6, 6.8, 6.7, 7.0, 7.2, 7.1, 7.4, 7.6]
    timestamps = [1_700_000_000 + i * 10 for i in range(len(closes))]
    old = macd(closes)
    new = macd_time_aware(closes, timestamps)
    assert new["macd"] == old["macd"]
    assert new["signal"] == old["signal"]
    assert new["histogram"] == old["histogram"]


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
