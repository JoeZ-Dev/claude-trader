import importlib.util
import os
import sys

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _APP_DIR)
_CORE = os.path.join(os.path.dirname(_APP_DIR), "core")
sys.path.insert(0, _CORE)

from indicators import ema, relative_volume, session_vwap  # from core
from levels import Level, detect_levels, evaluate_hold  # from core
from state import (
    LIVE_BAR_MAX_GAP_SECONDS,
    RELVOL_LOOKBACK,
    REQUIRED_HOLD_BARS,
    build_state,
    live_cadence_tail,
    select_levels,
    session_bars_for_vwap,
)


def _load_demo_session():
    spec = importlib.util.spec_from_file_location("_demo", os.path.join(_CORE, "demo.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return [{**b, "is_extended": False} for b in mod.build_session()]


def _lvl(price, kind, strength, touch=2):
    return Level(price=price, kind=kind, touch_count=touch,
                 total_touch_volume=100_000.0, last_touch_ts=0,
                 round_number_bonus=0.0, strength_score=strength)


# -- select_levels -------------------------------------------------------

def test_select_levels_picks_strongest_on_each_side():
    levels = [
        _lvl(11.0, "resistance", 5.0),
        _lvl(12.0, "resistance", 9.0),
        _lvl(9.0, "support", 4.0),
        _lvl(8.0, "support", 7.0),
    ]
    picked = select_levels(levels, current_price=10.0)
    assert picked["resistance"].price == 12.0
    assert picked["support"].price == 8.0


def test_select_levels_ignores_wrong_side_of_price():
    levels = [
        _lvl(9.5, "resistance", 20.0),   # below price -> not an eligible resistance
        _lvl(11.0, "resistance", 3.0),
        _lvl(10.5, "support", 20.0),      # above price -> not an eligible support
    ]
    picked = select_levels(levels, current_price=10.0)
    assert picked["resistance"].price == 11.0
    assert picked["support"] is None


def test_select_levels_none_when_no_levels():
    picked = select_levels([], current_price=10.0)
    assert picked == {"resistance": None, "support": None}


# -- build_state -------------------------------------------------------

def test_build_state_warming_up_on_empty():
    assert build_state([], symbol="AEHL")["status"] == "warming_up"


def test_build_state_on_demo_session_is_sane():
    bars = _load_demo_session()
    st = build_state(bars, symbol="AEHL")

    assert st["status"] == "ok"
    assert st["bar_count"] == len(bars)
    assert st["last_price"] == round(bars[-1]["close"], 4)

    lo = min(b["low"] for b in bars)
    hi = max(b["high"] for b in bars)
    assert lo <= st["session"]["vwap"] <= hi

    # The demo's own assertion: neither push on the top resistance held for
    # 3 consecutive closes, so hold-confirmation must be False.
    res = st["levels"]["resistance"]
    assert res is not None
    assert res["hold"]["confirmed"] is False


def test_build_state_exposes_level_components_separately():
    bars = _load_demo_session()
    res = build_state(bars)["levels"]["resistance"]
    comp = res["components"]
    assert set(comp) == {"touch_count", "total_touch_volume", "round_number_bonus"}
    assert isinstance(comp["touch_count"], int)


def test_build_state_hold_directions_are_above_for_resistance_below_for_support():
    # Price sits in a channel with a tested level on each side.
    bars = []
    ts = 0
    for _ in range(3):
        for p in (9.0, 9.5, 10.0, 9.6, 9.1):  # swing low ~9.0, swing high ~10.0
            bars.append({"ts": ts, "open": p, "high": p + 0.1, "low": p - 0.1,
                         "close": p, "volume": 50_000.0, "is_extended": False})
            ts += 10
    # settle in the middle
    bars.append({"ts": ts, "open": 9.5, "high": 9.55, "low": 9.45, "close": 9.5,
                 "volume": 40_000.0, "is_extended": False})

    st = build_state(bars, symbol="X")
    if st["levels"]["resistance"]:
        assert st["levels"]["resistance"]["hold"]["direction"] == "above"
    if st["levels"]["support"]:
        assert st["levels"]["support"]["hold"]["direction"] == "below"


def test_build_state_short_history_does_not_crash():
    bars = [{"ts": i * 10, "open": 5.0, "high": 5.1, "low": 4.9, "close": 5.0,
             "volume": 1000.0, "is_extended": False} for i in range(5)]
    st = build_state(bars, symbol="X")
    assert st["status"] == "ok"
    assert st["session"]["relative_volume"] == 1.0  # fewer than lookback bars
    assert "histogram" in st["session"]["macd"]


# -- live_cadence_tail ---------------------------------------------------

def test_live_cadence_tail_returns_everything_when_uniformly_spaced():
    bars = [{"ts": i * 10} for i in range(5)]
    assert live_cadence_tail(bars) == bars


def test_live_cadence_tail_isolates_segment_after_a_large_gap():
    backfill = [{"ts": t} for t in (0, 60, 120)]
    live = [{"ts": t} for t in (300, 310, 320)]
    assert live_cadence_tail(backfill + live) == live


def test_live_cadence_tail_empty_list():
    assert live_cadence_tail([]) == []


def test_live_cadence_tail_single_bar():
    bars = [{"ts": 100}]
    assert live_cadence_tail(bars) == bars


def test_live_cadence_tail_gap_exactly_at_threshold_still_counts_as_live():
    bars = [{"ts": 0}, {"ts": LIVE_BAR_MAX_GAP_SECONDS}]
    assert live_cadence_tail(bars) == bars


# -- build_state: backfill (coarse/irregular) vs. live (uniform 10s) -----
#
# Backfilled bars (schwab-connector/price_history.py) are Schwab
# price-history candles, no finer than 1 minute and with zero-volume
# minutes skipped entirely -- irregular, never as tight as 10s apart.
# Live bars (schwab-connector/aggregator.py) are always exactly
# BUCKET_SECONDS=10 apart once streaming starts. Bar-count-windowed
# functions (ema/macd/relative_volume/hold-confirmation) implicitly assume
# uniform bar width, so mixing the two would make a "9-period EMA" mean 9
# minutes one moment and 90 seconds the next. session_vwap and
# detect_levels are not window-based this way and are meant to see the
# whole session, backfill included -- see specs.md section 3 for the full
# rationale and the explicitly-deferred time-aware alternative.

def _flat_backfill_bars(price: float, count: int = 10, step: int = 60):
    return [{"ts": i * step, "open": price, "high": price, "low": price,
             "close": price, "volume": 5000.0, "is_extended": False}
            for i in range(count)]


def _flat_live_bars(price: float, start_ts: int, count: int = 5, step: int = 10):
    return [{"ts": start_ts + i * step, "open": price, "high": price,
             "low": price, "close": price, "volume": 500.0, "is_extended": False}
            for i in range(count)]


def test_build_state_ema_and_relative_volume_ignore_backfilled_bars():
    backfill = _flat_backfill_bars(100.0)  # would badly skew EMA/relvol if counted
    live = _flat_live_bars(10.0, backfill[-1]["ts"] + 300)
    bars = backfill + live

    st = build_state(bars, symbol="X")
    live_bars = live_cadence_tail(bars)
    assert live_bars == live  # sanity: the split landed where expected

    live_closes = [b["close"] for b in live_bars]
    assert st["session"]["ema9"] == round(ema(live_closes, 9)[-1], 4)
    assert st["session"]["relative_volume"] == round(
        relative_volume(live_bars, lookback=RELVOL_LOOKBACK)[-1], 4)
    # A 100.0-heavy EMA would round to 100.0-ish; confirm it doesn't.
    assert st["session"]["ema9"] < 50.0


def test_build_state_vwap_still_spans_the_full_backfilled_and_live_session():
    backfill = _flat_backfill_bars(100.0)
    live = _flat_live_bars(10.0, backfill[-1]["ts"] + 300)
    bars = backfill + live

    st = build_state(bars, symbol="X")
    live_only_vwap = session_vwap(live_cadence_tail(bars))[-1]
    # If VWAP only saw the live tail it would sit right at 10.0; seeing the
    # full session (10x more backfilled volume at 100.0) pulls it way up.
    assert st["session"]["vwap"] > live_only_vwap + 10.0


def test_build_state_hold_confirmation_uses_only_live_cadence_bars():
    # A dip mid-backfill gives detect_levels an obvious support candidate
    # whose backfilled closes alone would look "held" below it if counted;
    # the short live tail alone is too short to confirm anything.
    ts = 0
    prices = [10, 10, 9, 8, 6, 4, 6, 8, 9, 10]
    backfill = []
    for p in prices:
        backfill.append({"ts": ts, "open": p, "high": p + 0.2, "low": p - 0.2,
                         "close": p, "volume": 50_000.0, "is_extended": False})
        ts += 60
    live = _flat_live_bars(9.5, backfill[-1]["ts"] + 300, count=2)
    bars = backfill + live

    st = build_state(bars, symbol="X")
    live_bars = live_cadence_tail(bars)
    picked = select_levels(detect_levels(bars), bars[-1]["close"])

    for side, direction in (("resistance", "above"), ("support", "below")):
        level = picked[side]
        block = st["levels"][side]
        if level is None:
            assert block is None
            continue
        expected = evaluate_hold(live_bars, level.price, direction=direction,
                                 required_bars=REQUIRED_HOLD_BARS)
        assert block["hold"]["consecutive_bars"] == expected.consecutive_bars
        assert block["hold"]["confirmed"] == expected.confirmed


# -- setups (phase 3.5) -------------------------------------------------

def test_build_state_includes_setups_sorted_ascending_by_distance():
    bars = _load_demo_session()
    setups = build_state(bars, symbol="AEHL")["setups"]
    assert isinstance(setups, list)
    distances = [s["distance"] for s in setups]
    assert distances == sorted(distances)
    for s in setups:
        assert set(s) == {"setup_type", "trigger_price", "distance", "hold", "factors"}
        assert s["setup_type"] in {
            "resistance_breakout", "micro_breakout", "vwap_reclaim", "round_number_reclaim",
        }


def test_session_bars_for_vwap_slices_to_latest_ny_date():
    day1 = 1756909800            # 2025-09-03 10:30 ET
    day2 = day1 + 24 * 3600      # next day, same clock time
    bars = [
        {"ts": day1, "open": 1, "high": 1, "low": 1, "close": 1,
         "volume": 1.0, "is_extended": False},
        {"ts": day2, "open": 2, "high": 2, "low": 2, "close": 2,
         "volume": 1.0, "is_extended": False},
        {"ts": day2 + 10, "open": 3, "high": 3, "low": 3, "close": 3,
         "volume": 1.0, "is_extended": False},
    ]
    got = session_bars_for_vwap(bars)
    assert [b["ts"] for b in got] == [day2, day2 + 10]
