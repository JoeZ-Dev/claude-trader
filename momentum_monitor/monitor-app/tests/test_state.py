import importlib.util
import os
import sys

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _APP_DIR)
_CORE = os.path.join(os.path.dirname(_APP_DIR), "core")
sys.path.insert(0, _CORE)

from indicators import ema_time_aware, relative_volume_time_aware, session_vwap  # from core
from levels import Level, detect_levels, evaluate_hold_time_aware  # from core
from state import (
    RELVOL_LOOKBACK_SECONDS,
    REQUIRED_HOLD_SECONDS,
    build_state,
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


def test_build_state_exposes_session_cumulative_volume():
    # specs.md section 12's session-level volume gate needs today's
    # cumulative session volume -- the SAME session slice (session_bars_
    # for_vwap) VWAP already uses, not a separately-invented one.
    bars = _load_demo_session()
    st = build_state(bars, symbol="AEHL")
    session_bars = session_bars_for_vwap(bars)
    assert st["session"]["cumulative_volume"] == sum(b["volume"] for b in session_bars)


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


# -- build_state: backfilled+live series, all functions now time-aware --
#
# Migrated (phase 3.6 stage 3 part 2, specs.md section 19) OFF the old
# live_cadence_tail split -- ema/macd/relative_volume/hold-confirmation
# now all see the FULL backfilled+live series directly, via their
# time-aware versions, which correctly weight whatever cadence each bar
# actually has instead of needing a pre-filtered uniform-cadence subset.
# Backfilled bars (schwab-connector/price_history.py) are Schwab
# price-history candles, no finer than 1 minute and with zero-volume
# minutes skipped entirely -- irregular, never as tight as 10s apart.
# Live bars (schwab-connector/aggregator.py) are always exactly
# BUCKET_SECONDS=10 apart once streaming starts.

def _flat_backfill_bars(price: float, count: int = 10, step: int = 60):
    return [{"ts": i * step, "open": price, "high": price, "low": price,
             "close": price, "volume": 5000.0, "is_extended": False}
            for i in range(count)]


def _flat_live_bars(price: float, start_ts: int, count: int = 5, step: int = 10):
    return [{"ts": start_ts + i * step, "open": price, "high": price,
             "low": price, "close": price, "volume": 500.0, "is_extended": False}
            for i in range(count)]


def test_build_state_ema_and_relative_volume_now_include_backfilled_bars():
    # The migration's whole point: build_state's ema9/relative_volume must
    # match the time-aware functions run directly on the FULL bars list,
    # not a live-only tail -- and backfill must now measurably influence
    # the result (a realistic near-immediate transition gap, like real
    # AIFF's, not an artificial one large enough to itself decay the old
    # value away).
    backfill = _flat_backfill_bars(100.0)
    live = _flat_live_bars(10.0, backfill[-1]["ts"] + 10)
    bars = backfill + live

    st = build_state(bars, symbol="X")
    closes = [b["close"] for b in bars]
    timestamps = [b["ts"] for b in bars]
    assert st["session"]["ema9"] == round(ema_time_aware(closes, timestamps, 9)[-1], 4)
    assert st["session"]["relative_volume"] == round(
        relative_volume_time_aware(bars, lookback_seconds=RELVOL_LOOKBACK_SECONDS)[-1], 4)
    # The heavy backfilled volume at 100.0 now genuinely pulls the EMA up
    # well above the pure-live value (10.0), rather than being invisible.
    assert st["session"]["ema9"] > 30.0


def test_build_state_vwap_still_spans_the_full_backfilled_and_live_session():
    backfill = _flat_backfill_bars(100.0)
    live = _flat_live_bars(10.0, backfill[-1]["ts"] + 300)
    bars = backfill + live

    st = build_state(bars, symbol="X")
    live_only_vwap = session_vwap(live)[-1]
    # If VWAP only saw the live tail it would sit right at 10.0; seeing the
    # full session (10x more backfilled volume at 100.0) pulls it way up.
    # (VWAP was never live_cadence_tail-filtered -- unaffected by this
    # migration -- this test just reconfirms that's still true.)
    assert st["session"]["vwap"] > live_only_vwap + 10.0


def test_build_state_hold_confirmation_now_uses_the_full_bar_series():
    # A dip mid-backfill gives detect_levels an obvious support candidate;
    # build_state's hold block must match evaluate_hold_time_aware run
    # directly on the FULL bars list.
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
    picked = select_levels(detect_levels(bars), bars[-1]["close"])

    for side, direction in (("resistance", "above"), ("support", "below")):
        level = picked[side]
        block = st["levels"][side]
        if level is None:
            assert block is None
            continue
        expected = evaluate_hold_time_aware(bars, level.price, direction=direction,
                                            required_seconds=REQUIRED_HOLD_SECONDS)
        assert block["hold"]["elapsed_seconds"] == expected.elapsed_seconds
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


# -- breakdown-below variants (specs.md section 22) -------------------
# Structurally SEPARATE from `setups` -- warning/context signals only,
# never a trade trigger (see monitor-app/tests/test_journal_logic.py's
# structural safety proof and monitor-app/app.py's _update_journal, which
# only ever reads `setups`, never `breakdown_setups`).

def test_build_state_includes_breakdown_setups_as_a_separate_key():
    bars = _load_demo_session()
    st = build_state(bars, symbol="AEHL")
    assert "breakdown_setups" in st
    # never overlaps the bullish `setups` list's own key set/types
    bullish_types = {s["setup_type"] for s in st["setups"]}
    breakdown_types = {s["setup_type"] for s in st["breakdown_setups"]}
    assert bullish_types.isdisjoint(breakdown_types)


def test_build_state_breakdown_setups_always_includes_round_number_breakdown():
    # round_number_breakdown has no gating condition (mirrors round_number_
    # reclaim's own "always present" nature) -- always watchable regardless
    # of trend, so any "ok" session has at least this one breakdown type.
    bars = _load_demo_session()
    st = build_state(bars, symbol="AEHL")
    types = {s["setup_type"] for s in st["breakdown_setups"]}
    assert "round_number_breakdown" in types


def test_build_state_breakdown_setups_sorted_ascending_by_distance():
    bars = _load_demo_session()
    breakdown_setups = build_state(bars, symbol="AEHL")["breakdown_setups"]
    distances = [s["distance"] for s in breakdown_setups]
    assert distances == sorted(distances)
    for s in breakdown_setups:
        assert set(s) == {"setup_type", "trigger_price", "distance", "hold", "factors"}
        assert s["setup_type"] in {
            "support_breakdown", "micro_breakdown", "vwap_breakdown", "round_number_breakdown",
        }


def test_build_state_breakdown_setups_hold_direction_is_below():
    bars = _load_demo_session()
    breakdown_setups = build_state(bars, symbol="AEHL")["breakdown_setups"]
    assert breakdown_setups  # sanity: round_number_breakdown guarantees >= 1
    for s in breakdown_setups:
        assert s["hold"]["direction"] == "below"


def test_build_state_watch_added_ts_reaches_breakdown_setups_too():
    # Same backfill-only-confirmation guard as the bullish setups (specs.md
    # section 19) -- must actually reach evaluate_breakdown_setups' own
    # watch_added_ts parameter, not just setup_types.py's bullish path.
    bars = [
        {"ts": 0, "open": 1.3, "high": 1.35, "low": 1.25, "close": 1.29,
         "volume": 10_000.0, "is_extended": False},
        {"ts": 10, "open": 1.29, "high": 1.31, "low": 1.24, "close": 1.28,
         "volume": 10_000.0, "is_extended": False},
        {"ts": 20, "open": 1.28, "high": 1.30, "low": 1.23, "close": 1.27,
         "volume": 10_000.0, "is_extended": False},
        # closes back below 1.30 -- confirmation (if any) already happened
        # by ts=20.
        {"ts": 30, "open": 1.27, "high": 1.29, "low": 1.22, "close": 1.31,
         "volume": 10_000.0, "is_extended": False},
    ]
    unrestricted = build_state(bars, symbol="X")
    breakdown = next(s for s in unrestricted["breakdown_setups"]
                     if s["setup_type"] == "round_number_breakdown")
    assert breakdown["hold"]["confirmed"] is True

    restricted = build_state(bars, symbol="X", watch_added_ts=25)
    breakdown_restricted = next(s for s in restricted["breakdown_setups"]
                                if s["setup_type"] == "round_number_breakdown")
    assert breakdown_restricted["hold"]["confirmed"] is False


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


# -- watch_added_ts (phase 3.6 stage 3 part 2, specs.md section 19) ------

def test_build_state_watch_added_ts_reaches_setups_and_level_blocks():
    # A round-number reclaim (and, by the same code path, the resistance/
    # support hold blocks) that would otherwise confirm entirely within
    # pre-watch bars must not show confirmed=True when watch_added_ts is
    # given -- must actually reach build_state's own call sites, not just
    # setup_types.py's in isolation.
    bars = [
        {"ts": 0, "open": 1.3, "high": 1.35, "low": 1.25, "close": 1.32,
         "volume": 10_000.0, "is_extended": False},
        {"ts": 10, "open": 1.32, "high": 1.36, "low": 1.28, "close": 1.33,
         "volume": 10_000.0, "is_extended": False},
        {"ts": 20, "open": 1.33, "high": 1.37, "low": 1.29, "close": 1.34,
         "volume": 10_000.0, "is_extended": False},
        # closes below the 1.30 trigger the FINAL close itself derives --
        # confirmation (if any) must have already happened at ts=20.
        {"ts": 30, "open": 1.3, "high": 1.32, "low": 1.24, "close": 1.29,
         "volume": 10_000.0, "is_extended": False},
    ]
    unrestricted = build_state(bars, symbol="X")
    reclaim = next(s for s in unrestricted["setups"] if s["setup_type"] == "round_number_reclaim")
    assert reclaim["hold"]["confirmed"] is True  # the real risk, reproduced at build_state level

    restricted = build_state(bars, symbol="X", watch_added_ts=1_000_000)
    reclaim_restricted = next(s for s in restricted["setups"] if s["setup_type"] == "round_number_reclaim")
    assert reclaim_restricted["hold"]["confirmed"] is False
