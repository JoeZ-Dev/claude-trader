import os
import sys

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _APP_DIR)

from journal_logic import (
    ExitEvent,
    OpenPosition,
    advance_journal,
    apply_bar_to_open_position,
    initial_stop_level,
    should_enter,
)

TRAIL_PCT = 0.05


def _bar(ts, *, high, low, close, open_=None):
    o = open_ if open_ is not None else close
    return {"ts": ts, "open": o, "high": high, "low": low, "close": close,
            "volume": 1000.0, "is_extended": False}


# -- initial_stop_level ----------------------------------------------------

def test_initial_stop_level_is_trail_pct_below_entry():
    assert initial_stop_level(100.0, 0.05) == 95.0


# -- should_enter ------------------------------------------------------------

def test_should_enter_on_false_to_true_transition_with_no_open_position():
    assert should_enter(was_confirmed_before=False, is_confirmed_now=True,
                        position_open=False) is True


def test_should_not_enter_when_already_confirmed_last_poll_too():
    # no FRESH transition -- was already True
    assert should_enter(was_confirmed_before=True, is_confirmed_now=True,
                        position_open=False) is False


def test_should_not_enter_when_not_confirmed_now():
    assert should_enter(was_confirmed_before=False, is_confirmed_now=False,
                        position_open=False) is False


def test_should_not_enter_when_position_already_open_even_on_fresh_transition():
    # guards the "only one open position at a time" invariant even if the
    # transition bookkeeping somehow disagreed with position state
    assert should_enter(was_confirmed_before=False, is_confirmed_now=True,
                        position_open=True) is False


# -- apply_bar_to_open_position: high-water-mark ratchet --------------------

def test_high_water_mark_starts_at_entry_price_not_bar_high():
    pos = OpenPosition(id=None, symbol="X", entry_ts=0, entry_price=100.0,
                       high_water_mark=100.0, stop_level=initial_stop_level(100.0, TRAIL_PCT))
    assert pos.high_water_mark == 100.0


def test_high_water_mark_ratchets_up_on_new_high():
    pos = OpenPosition(id=None, symbol="X", entry_ts=0, entry_price=100.0,
                       high_water_mark=100.0, stop_level=95.0)
    updated, exit_event = apply_bar_to_open_position(
        pos, _bar(10, high=110.0, low=105.0, close=108.0), TRAIL_PCT)
    assert updated.high_water_mark == 110.0
    assert updated.stop_level == 110.0 * (1 - TRAIL_PCT)
    assert exit_event is None


def test_high_water_mark_never_moves_down_on_a_pullback():
    pos = OpenPosition(id=None, symbol="X", entry_ts=0, entry_price=100.0,
                       high_water_mark=120.0, stop_level=120.0 * (1 - TRAIL_PCT))
    # a bar that pulls back (high stays below the existing 120 high-water
    # mark, so no ratchet; low stays above the existing 120*0.95=114 stop)
    updated, exit_event = apply_bar_to_open_position(
        pos, _bar(10, high=118.0, low=116.0, close=117.0), TRAIL_PCT)
    assert updated.high_water_mark == 120.0          # unchanged, not 115
    assert updated.stop_level == 120.0 * (1 - TRAIL_PCT)  # unchanged
    assert exit_event is None


def test_stop_level_recomputed_every_ratchet_from_the_new_high_water_mark():
    pos = OpenPosition(id=None, symbol="X", entry_ts=0, entry_price=100.0,
                       high_water_mark=100.0, stop_level=95.0)
    updated, _ = apply_bar_to_open_position(
        pos, _bar(10, high=200.0, low=190.0, close=195.0), TRAIL_PCT)
    assert updated.stop_level == 200.0 * (1 - TRAIL_PCT)


# -- apply_bar_to_open_position: stop breach, unconditional, no confirmation -

def test_stop_exit_fires_immediately_on_low_crossing_stop_no_confirmation_bars():
    # Regression guard: this must fire on the FIRST bar whose low breaches
    # the stop -- if someone accidentally copied the entry side's
    # hold-confirmation pattern here (requiring N consecutive bars before
    # trusting the breach), this test fails. Stops fire fast, no exceptions
    # (specs.md section 3's existing, deliberate asymmetry).
    pos = OpenPosition(id=None, symbol="X", entry_ts=0, entry_price=100.0,
                       high_water_mark=100.0, stop_level=95.0)
    updated, exit_event = apply_bar_to_open_position(
        pos, _bar(10, high=96.0, low=94.0, close=94.5), TRAIL_PCT)
    assert exit_event is not None
    assert exit_event.exit_reason == "trailing_stop"
    # A single breaching bar is enough -- there is no "required_bars"-style
    # parameter on this function's signature at all, and no second call is
    # needed to confirm the breach.
    assert exit_event.exit_ts == 10


def test_stop_exit_uses_the_freshly_ratcheted_stop_not_the_pre_bar_one():
    # Worst-case-first: if a single bar both makes a new high (raising the
    # stop) AND has a low that breaches that NEW stop, the exit must still
    # fire -- checking against the stale pre-bar stop would miss it.
    pos = OpenPosition(id=None, symbol="X", entry_ts=0, entry_price=100.0,
                       high_water_mark=100.0, stop_level=95.0)
    # high=110 -> new stop = 110*0.95 = 104.5; low=100 breaches 104.5 but
    # NOT the old stop of 95.
    updated, exit_event = apply_bar_to_open_position(
        pos, _bar(10, high=110.0, low=100.0, close=105.0), TRAIL_PCT)
    assert exit_event is not None
    assert updated.high_water_mark == 110.0


def test_no_exit_when_low_stays_above_stop():
    pos = OpenPosition(id=None, symbol="X", entry_ts=0, entry_price=100.0,
                       high_water_mark=100.0, stop_level=95.0)
    # high=101 ratchets the stop to 101*0.95=95.95; low=96.5 stays above it
    updated, exit_event = apply_bar_to_open_position(
        pos, _bar(10, high=101.0, low=96.5, close=100.5), TRAIL_PCT)
    assert exit_event is None


def test_exit_price_is_the_stop_level_not_the_bar_low():
    # Modeling choice for a virtual/simulated fill: assume the stop fills
    # at the stop price itself, not worst-case at the bar's low. Documented
    # as a choice, not a guaranteed real-fill guarantee.
    pos = OpenPosition(id=None, symbol="X", entry_ts=0, entry_price=100.0,
                       high_water_mark=100.0, stop_level=95.0)
    _, exit_event = apply_bar_to_open_position(
        pos, _bar(10, high=96.0, low=90.0, close=91.0), TRAIL_PCT)
    assert exit_event.exit_price == 95.0


# -- advance_journal: the per-poll orchestration function -------------------

def test_advance_journal_opens_a_new_position_on_fresh_confirmation():
    tick = advance_journal(
        position=None,
        new_bars=[_bar(100, high=10.5, low=9.8, close=10.2)],
        is_confirmed_now=True, was_confirmed_before=False,
        trail_pct=TRAIL_PCT, symbol="AEHL",
    )
    assert tick.opened is not None
    assert tick.opened.entry_price == 10.2
    assert tick.opened.entry_ts == 100
    assert tick.opened.high_water_mark == 10.2
    assert tick.opened.stop_level == 10.2 * (1 - TRAIL_PCT)
    assert tick.closed is None
    assert tick.was_confirmed_after is True


def test_advance_journal_does_not_open_a_duplicate_when_already_confirmed():
    tick = advance_journal(
        position=None,
        new_bars=[_bar(100, high=10.5, low=9.8, close=10.2)],
        is_confirmed_now=True, was_confirmed_before=True,   # no fresh transition
        trail_pct=TRAIL_PCT, symbol="AEHL",
    )
    assert tick.opened is None


def test_advance_journal_updates_open_position_across_multiple_new_bars():
    pos = OpenPosition(id=1, symbol="AEHL", entry_ts=0, entry_price=10.0,
                       high_water_mark=10.0, stop_level=9.5)
    bars = [
        _bar(10, high=10.5, low=10.1, close=10.4),
        _bar(20, high=11.0, low=10.6, close=10.9),
    ]
    tick = advance_journal(
        position=pos, new_bars=bars, is_confirmed_now=True,
        was_confirmed_before=True, trail_pct=TRAIL_PCT, symbol="AEHL",
    )
    assert tick.updated is not None
    assert tick.updated.high_water_mark == 11.0
    assert tick.updated.stop_level == 11.0 * (1 - TRAIL_PCT)
    assert tick.closed is None
    assert tick.opened is None


def test_advance_journal_closes_position_the_moment_a_bar_breaches_stop():
    pos = OpenPosition(id=1, symbol="AEHL", entry_ts=0, entry_price=10.0,
                       high_water_mark=10.0, stop_level=9.5)
    bars = [
        _bar(10, high=10.2, low=10.0, close=10.1),   # fine
        _bar(20, high=10.2, low=9.4, close=9.5),      # breaches 9.5
        _bar(30, high=9.6, low=9.5, close=9.55),      # should never be reached
    ]
    tick = advance_journal(
        position=pos, new_bars=bars, is_confirmed_now=True,
        was_confirmed_before=True, trail_pct=TRAIL_PCT, symbol="AEHL",
    )
    assert tick.closed is not None
    closed_position, exit_event = tick.closed
    assert exit_event.exit_reason == "trailing_stop"
    assert exit_event.exit_ts == 20      # the breaching bar, not the third one
    assert tick.updated is None


def test_advance_journal_no_bars_is_a_safe_noop():
    tick = advance_journal(
        position=None, new_bars=[], is_confirmed_now=False,
        was_confirmed_before=False, trail_pct=TRAIL_PCT, symbol="AEHL",
    )
    assert tick.opened is None
    assert tick.updated is None
    assert tick.closed is None
