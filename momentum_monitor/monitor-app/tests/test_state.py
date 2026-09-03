import importlib.util
import os
import sys

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _APP_DIR)
_CORE = os.path.join(os.path.dirname(_APP_DIR), "core")
sys.path.insert(0, _CORE)

from levels import Level  # from core, via _CORE on sys.path
from state import build_state, select_levels, session_bars_for_vwap


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
