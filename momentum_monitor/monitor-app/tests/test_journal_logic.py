import os
import sys

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _APP_DIR)

import pytest

from journal_logic import (
    ExitEvent,
    OpenPosition,
    advance_journal,
    apply_bar_to_open_position,
    initial_stop_level,
    should_enter,
)

TRAIL_PCT = 0.05
VOLUME_CONFIRM_THRESHOLD = 1.5
HIGH_VOLUME = 2.0   # clears the threshold
LOW_VOLUME = 1.0    # does not


def _bar(ts, *, high, low, close, open_=None):
    o = open_ if open_ is not None else close
    return {"ts": ts, "open": o, "high": high, "low": low, "close": close,
            "volume": 1000.0, "is_extended": False}


def _setup(setup_type, *, confirmed, distance=1.0, trigger_price=10.5, **factors):
    return {
        "setup_type": setup_type,
        "trigger_price": trigger_price,
        "distance": distance,
        "hold": {"direction": "above", "required_bars": 3,
                 "consecutive_bars": 3 if confirmed else 1,
                 "confirmed": confirmed, "failed_attempts": 0},
        "factors": factors or {"strength_score": 5.0, "touch_count": 2},
    }


# -- initial_stop_level ----------------------------------------------------

def test_initial_stop_level_is_trail_pct_below_entry():
    assert initial_stop_level(100.0, 0.05) == 95.0


# -- should_enter ------------------------------------------------------------

def test_should_enter_on_fresh_confirmation_with_volume_and_no_open_position():
    assert should_enter(
        newly_confirmed_type="resistance_breakout", relative_volume=HIGH_VOLUME,
        volume_confirm_threshold=VOLUME_CONFIRM_THRESHOLD, position_open=False,
    ) is True


def test_should_not_enter_when_nothing_newly_confirmed():
    assert should_enter(
        newly_confirmed_type=None, relative_volume=HIGH_VOLUME,
        volume_confirm_threshold=VOLUME_CONFIRM_THRESHOLD, position_open=False,
    ) is False


def test_should_not_enter_when_position_already_open_even_on_fresh_transition():
    # guards the "only one open position at a time" invariant even if the
    # transition bookkeeping somehow disagreed with position state
    assert should_enter(
        newly_confirmed_type="resistance_breakout", relative_volume=HIGH_VOLUME,
        volume_confirm_threshold=VOLUME_CONFIRM_THRESHOLD, position_open=True,
    ) is False


def test_should_not_enter_when_volume_is_below_threshold():
    assert should_enter(
        newly_confirmed_type="resistance_breakout", relative_volume=LOW_VOLUME,
        volume_confirm_threshold=VOLUME_CONFIRM_THRESHOLD, position_open=False,
    ) is False


def test_should_enter_when_volume_is_exactly_at_threshold():
    assert should_enter(
        newly_confirmed_type="resistance_breakout",
        relative_volume=VOLUME_CONFIRM_THRESHOLD,
        volume_confirm_threshold=VOLUME_CONFIRM_THRESHOLD, position_open=False,
    ) is True


# -- apply_bar_to_open_position: high-water-mark ratchet --------------------

def test_high_water_mark_starts_at_entry_price_not_bar_high():
    pos = OpenPosition(id=None, symbol="X", entry_ts=0, entry_price=100.0,
                       high_water_mark=100.0, stop_level=initial_stop_level(100.0, TRAIL_PCT))
    assert pos.high_water_mark == 100.0


def test_high_water_mark_ratchets_up_on_new_high():
    pos = OpenPosition(id=None, symbol="X", entry_ts=0, entry_price=100.0,
                       high_water_mark=100.0, stop_level=95.0)
    updated, exit_event = apply_bar_to_open_position(
        pos, _bar(10, high=110.0, low=105.0, close=108.0))
    assert updated.high_water_mark == 110.0
    assert updated.stop_level == 110.0 * (1 - TRAIL_PCT)
    assert exit_event is None


def test_high_water_mark_never_moves_down_on_a_pullback():
    pos = OpenPosition(id=None, symbol="X", entry_ts=0, entry_price=100.0,
                       high_water_mark=120.0, stop_level=120.0 * (1 - TRAIL_PCT))
    # a bar that pulls back (high stays below the existing 120 high-water
    # mark, so no ratchet; low stays above the existing 120*0.95=114 stop)
    updated, exit_event = apply_bar_to_open_position(
        pos, _bar(10, high=118.0, low=116.0, close=117.0))
    assert updated.high_water_mark == 120.0          # unchanged, not 115
    assert updated.stop_level == 120.0 * (1 - TRAIL_PCT)  # unchanged
    assert exit_event is None


def test_stop_level_recomputed_every_ratchet_from_the_new_high_water_mark():
    pos = OpenPosition(id=None, symbol="X", entry_ts=0, entry_price=100.0,
                       high_water_mark=100.0, stop_level=95.0)
    updated, _ = apply_bar_to_open_position(
        pos, _bar(10, high=200.0, low=190.0, close=195.0))
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
        pos, _bar(10, high=96.0, low=94.0, close=94.5))
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
        pos, _bar(10, high=110.0, low=100.0, close=105.0))
    assert exit_event is not None
    assert updated.high_water_mark == 110.0


def test_no_exit_when_low_stays_above_stop():
    pos = OpenPosition(id=None, symbol="X", entry_ts=0, entry_price=100.0,
                       high_water_mark=100.0, stop_level=95.0)
    # high=101 ratchets the stop to 101*0.95=95.95; low=96.5 stays above it
    updated, exit_event = apply_bar_to_open_position(
        pos, _bar(10, high=101.0, low=96.5, close=100.5))
    assert exit_event is None


def test_exit_price_is_the_stop_level_not_the_bar_low():
    # Modeling choice for a virtual/simulated fill: assume the stop fills
    # at the stop price itself, not worst-case at the bar's low. Documented
    # as a choice, not a guaranteed real-fill guarantee.
    pos = OpenPosition(id=None, symbol="X", entry_ts=0, entry_price=100.0,
                       high_water_mark=100.0, stop_level=95.0)
    _, exit_event = apply_bar_to_open_position(
        pos, _bar(10, high=96.0, low=90.0, close=91.0))
    assert exit_event.exit_price == 95.0


# -- advance_journal: the per-poll orchestration function -------------------

_ALL_FOUR_TYPES = ("resistance_breakout", "micro_breakout",
                   "vwap_reclaim", "round_number_reclaim")


def _advance(*, position=None, new_bars, setups, was_confirmed_types=frozenset(),
            relative_volume=HIGH_VOLUME,
            volume_confirm_threshold=VOLUME_CONFIRM_THRESHOLD, symbol="AEHL",
            trail_pct=TRAIL_PCT, watch_note=None):
    return advance_journal(
        position=position, new_bars=new_bars, setups=setups,
        was_confirmed_types=was_confirmed_types, relative_volume=relative_volume,
        volume_confirm_threshold=volume_confirm_threshold,
        trail_pct=trail_pct, symbol=symbol, watch_note=watch_note,
    )


def test_advance_journal_opens_a_new_position_on_fresh_confirmation():
    tick = _advance(
        new_bars=[_bar(100, high=10.5, low=9.8, close=10.2)],
        setups=[_setup("resistance_breakout", confirmed=True)],
    )
    assert tick.opened is not None
    assert tick.opened.entry_price == 10.2
    assert tick.opened.entry_ts == 100
    assert tick.opened.high_water_mark == 10.2
    assert tick.opened.stop_level == 10.2 * (1 - TRAIL_PCT)
    assert tick.closed is None
    assert tick.confirmed_types_after == {"resistance_breakout"}


def test_advance_journal_does_not_open_a_duplicate_when_already_confirmed():
    tick = _advance(
        new_bars=[_bar(100, high=10.5, low=9.8, close=10.2)],
        setups=[_setup("resistance_breakout", confirmed=True)],
        was_confirmed_types={"resistance_breakout"},   # no fresh transition
    )
    assert tick.opened is None


# -- Part A: entry generalized to ALL four setup types, independently ------

@pytest.mark.parametrize("setup_type", _ALL_FOUR_TYPES)
def test_advance_journal_opens_on_each_setup_type_independently(setup_type):
    tick = _advance(
        new_bars=[_bar(100, high=10.5, low=9.8, close=10.2)],
        setups=[_setup(setup_type, confirmed=True)],
    )
    assert tick.opened is not None
    assert tick.opened.setup_type == setup_type


def test_advance_journal_a_second_type_confirming_later_still_fires_its_own_entry():
    # resistance_breakout confirmed and fired (and, in this scenario, has
    # since closed) an earlier trade; it's STILL sitting confirmed=True.
    # micro_breakout confirming now is a fresh transition for micro_
    # breakout specifically, and must fire its own entry -- this is
    # exactly what a single collapsed "was anything confirmed" boolean
    # would have missed (see journal_logic.py module docstring).
    tick = _advance(
        new_bars=[_bar(100, high=10.5, low=9.8, close=10.2)],
        setups=[
            _setup("resistance_breakout", confirmed=True, distance=2.0),
            _setup("micro_breakout", confirmed=True, distance=0.5),
        ],
        was_confirmed_types={"resistance_breakout"},
    )
    assert tick.opened is not None
    assert tick.opened.setup_type == "micro_breakout"


def test_advance_journal_ties_go_to_the_closest_setup():
    # setups is pre-sorted ascending by distance (evaluate_setups' own
    # contract) -- when two types confirm in the same tick, the first
    # (closest) one in that order wins, deterministically.
    tick = _advance(
        new_bars=[_bar(100, high=10.5, low=9.8, close=10.2)],
        setups=[
            _setup("micro_breakout", confirmed=True, distance=0.2),
            _setup("resistance_breakout", confirmed=True, distance=1.0),
        ],
    )
    assert tick.opened.setup_type == "micro_breakout"


def test_advance_journal_still_guards_against_a_duplicate_open_position():
    pos = OpenPosition(id=1, symbol="AEHL", entry_ts=0, entry_price=10.0,
                       high_water_mark=10.0, stop_level=9.5)
    tick = _advance(
        position=pos,
        new_bars=[_bar(10, high=10.2, low=10.1, close=10.15)],
        setups=[_setup("vwap_reclaim", confirmed=True)],
    )
    assert tick.opened is None
    assert tick.updated is not None  # the existing position just ratcheted


# -- Part B: volume confirmation gates entries only, never exits -----------

def test_advance_journal_blocks_entry_when_relative_volume_below_threshold():
    # Same setup, same fresh confirmation -- the ONLY difference from
    # test_advance_journal_opens_a_new_position_on_fresh_confirmation is
    # volume. Proves the gate actually blocks something, not just that it
    # exists: this exact scenario fires under the old, ungated logic.
    tick = _advance(
        new_bars=[_bar(100, high=10.5, low=9.8, close=10.2)],
        setups=[_setup("resistance_breakout", confirmed=True)],
        relative_volume=LOW_VOLUME,
    )
    assert tick.opened is None
    # still marked "seen" -- doesn't get a second chance later while it
    # stays confirmed with the same low volume
    assert tick.confirmed_types_after == {"resistance_breakout"}


def test_advance_journal_allows_entry_once_volume_clears_threshold():
    tick = _advance(
        new_bars=[_bar(100, high=10.5, low=9.8, close=10.2)],
        setups=[_setup("resistance_breakout", confirmed=True)],
        relative_volume=HIGH_VOLUME,
    )
    assert tick.opened is not None


def test_advance_journal_exit_is_never_gated_by_volume():
    # A position already open, breaching its stop, with LOW relative
    # volume on the breaching bar -- the exit must fire exactly as if
    # volume were high. Stops stay fast and unconditional, no exceptions,
    # same asymmetry core/ has always used (specs.md section 3).
    pos = OpenPosition(id=1, symbol="AEHL", entry_ts=0, entry_price=10.0,
                       high_water_mark=10.0, stop_level=9.5)
    tick = _advance(
        position=pos,
        new_bars=[_bar(20, high=10.2, low=9.4, close=9.45)],  # breaches 9.5
        setups=[],
        relative_volume=LOW_VOLUME,
    )
    assert tick.closed is not None
    assert tick.closed[1].exit_reason == "trailing_stop"


# -- Part C: setup_type / factors captured at the moment of entry ----------

def test_advance_journal_records_setup_type_and_merged_factors_on_entry():
    tick = _advance(
        new_bars=[_bar(100, high=10.5, low=9.8, close=10.2)],
        setups=[_setup("resistance_breakout", confirmed=True, distance=1.25,
                       trigger_price=11.45, strength_score=7.5, touch_count=3)],
        relative_volume=1.8,
    )
    assert tick.opened.setup_type == "resistance_breakout"
    assert tick.opened.factors["strength_score"] == 7.5
    assert tick.opened.factors["touch_count"] == 3
    assert tick.opened.factors["distance"] == 1.25
    assert tick.opened.factors["trigger_price"] == 11.45
    assert tick.opened.factors["relative_volume"] == 1.8


# -- live-tunable strategy_params: locked in at entry, not re-read live ---

def test_new_entry_uses_the_current_trail_pct_and_locks_it_onto_the_position():
    tick = _advance(
        new_bars=[_bar(100, high=10.5, low=9.8, close=10.2)],
        setups=[_setup("resistance_breakout", confirmed=True)],
        trail_pct=0.10,  # NOT the module TRAIL_PCT=0.05 default
    )
    assert tick.opened.trail_pct == 0.10
    assert tick.opened.stop_level == 10.2 * (1 - 0.10)
    assert tick.opened.volume_threshold_used == VOLUME_CONFIRM_THRESHOLD


def test_open_positions_ratchet_uses_its_own_locked_trail_pct_not_a_new_global():
    # Position entered under trail_pct=0.05 (10.0 * 0.95 = 9.5). A
    # parameter change to 0.20 happens (simulated: advance_journal is
    # called with the NEW global) while this position is still open --
    # its own ratchet math must still use 0.05, never 0.20, per specs.md
    # section 8's "not affected by subsequent parameter changes."
    pos = OpenPosition(id=1, symbol="AEHL", entry_ts=0, entry_price=10.0,
                       high_water_mark=10.0, stop_level=9.5, trail_pct=0.05)
    tick = _advance(
        position=pos,
        new_bars=[_bar(10, high=11.0, low=10.6, close=10.9)],
        setups=[], trail_pct=0.20,  # a changed global, must be ignored here
    )
    assert tick.updated.high_water_mark == 11.0
    assert tick.updated.stop_level == 11.0 * (1 - 0.05)  # locked 0.05, not 0.20
    assert tick.updated.trail_pct == 0.05


# -- watch_note snapshot (specs.md section 7's highest-priority gap) -------

def test_new_entry_snapshots_the_current_watch_note():
    tick = _advance(
        new_bars=[_bar(100, high=10.5, low=9.8, close=10.2)],
        setups=[_setup("resistance_breakout", confirmed=True)],
        watch_note="halted then reopened on FDA news, watching for reclaim",
    )
    assert tick.opened.watch_note == "halted then reopened on FDA news, watching for reclaim"


def test_new_entry_with_no_note_recorded_is_none_not_empty_string():
    tick = _advance(
        new_bars=[_bar(100, high=10.5, low=9.8, close=10.2)],
        setups=[_setup("resistance_breakout", confirmed=True)],
        watch_note=None,
    )
    assert tick.opened.watch_note is None


def test_advance_journal_updates_open_position_across_multiple_new_bars():
    pos = OpenPosition(id=1, symbol="AEHL", entry_ts=0, entry_price=10.0,
                       high_water_mark=10.0, stop_level=9.5)
    bars = [
        _bar(10, high=10.5, low=10.1, close=10.4),
        _bar(20, high=11.0, low=10.6, close=10.9),
    ]
    tick = _advance(position=pos, new_bars=bars, setups=[])
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
    tick = _advance(position=pos, new_bars=bars, setups=[])
    assert tick.closed is not None
    closed_position, exit_event = tick.closed
    assert exit_event.exit_reason == "trailing_stop"
    assert exit_event.exit_ts == 20      # the breaching bar, not the third one
    assert tick.updated is None


def test_advance_journal_can_both_close_and_reopen_within_one_batch():
    # A stop-out followed immediately, within the SAME batch of new_bars,
    # by a fresh confirmation (e.g. round_number_reclaim re-confirming on
    # the next round-number grid point right after a stop-out) -- both
    # halves are real and JournalTick must report both; found live via
    # generalizing entry to all four setup types (round_number_reclaim's
    # "always present" nature makes this a real, not hypothetical,
    # sequence), and the exact thing app.py's _update_journal must not
    # silently drop the close half of just because opened is ALSO set.
    pos = OpenPosition(id=1, symbol="AEHL", entry_ts=0, entry_price=10.0,
                       high_water_mark=10.0, stop_level=9.5)
    bars = [
        _bar(10, high=10.2, low=9.4, close=9.45),    # breaches 9.5, closes it
        _bar(20, high=10.6, low=10.5, close=10.55),  # fresh confirmation bar
    ]
    tick = _advance(
        position=pos, new_bars=bars,
        setups=[_setup("round_number_reclaim", confirmed=True, distance=0.1)],
    )
    assert tick.closed is not None
    assert tick.closed[1].exit_reason == "trailing_stop"
    assert tick.opened is not None
    assert tick.opened.setup_type == "round_number_reclaim"
    assert tick.opened.entry_price == 10.55
    assert tick.opened.entry_ts == 20


def test_advance_journal_no_bars_is_a_safe_noop():
    tick = _advance(new_bars=[], setups=[])
    assert tick.opened is None
    assert tick.updated is None
    assert tick.closed is None
