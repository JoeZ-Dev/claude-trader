import math
import os
import sys
from dataclasses import replace

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
CURRENT_EQUITY = 2000.0
RISK_PCT_PER_TRADE = 0.01
SWING_LOW_BUFFER_PCT = 0.005
PATTERN_PROGRESS_THRESHOLD_PCT = 0.03
SESSION_VOLUME_MULTIPLE = 3.0
# session_cumulative_volume's default, paired with avg_daily_volume's own
# advance_journal default of None below -- None already means "skip the
# gate" (should_enter's documented behavior), so this value is inert
# unless a test explicitly overrides avg_daily_volume too.
SESSION_CUMULATIVE_VOLUME = 0.0


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


# -- should_enter: session-level volume gate, specs.md section 12 ----------
# (separate from, and stacking with, the bar-level relative_volume gate
# above -- every test here uses HIGH_VOLUME/VOLUME_CONFIRM_THRESHOLD so
# that gate always passes, isolating what's actually under test.)

def test_should_enter_default_kwargs_skip_the_session_volume_gate():
    # Existing callers that don't pass the three new kwargs at all keep
    # working exactly as before -- avg_daily_volume defaults to None,
    # which skips this gate entirely.
    assert should_enter(
        newly_confirmed_type="resistance_breakout", relative_volume=HIGH_VOLUME,
        volume_confirm_threshold=VOLUME_CONFIRM_THRESHOLD, position_open=False,
    ) is True


def test_should_enter_skips_the_gate_when_avg_daily_volume_is_none():
    # The real value whenever historical data couldn't be fetched
    # (specs.md section 12's explicit "skip, don't block" choice) --
    # even a tiny session_cumulative_volume doesn't block entry.
    assert should_enter(
        newly_confirmed_type="resistance_breakout", relative_volume=HIGH_VOLUME,
        volume_confirm_threshold=VOLUME_CONFIRM_THRESHOLD, position_open=False,
        session_cumulative_volume=1.0, avg_daily_volume=None,
        session_volume_multiple=3.0,
    ) is True


def test_should_enter_blocks_when_session_volume_below_the_multiple():
    assert should_enter(
        newly_confirmed_type="resistance_breakout", relative_volume=HIGH_VOLUME,
        volume_confirm_threshold=VOLUME_CONFIRM_THRESHOLD, position_open=False,
        session_cumulative_volume=100_000.0, avg_daily_volume=50_000.0,
        session_volume_multiple=3.0,  # needs >= 150_000
    ) is False


def test_should_enter_allows_when_session_volume_exactly_at_the_multiple():
    assert should_enter(
        newly_confirmed_type="resistance_breakout", relative_volume=HIGH_VOLUME,
        volume_confirm_threshold=VOLUME_CONFIRM_THRESHOLD, position_open=False,
        session_cumulative_volume=150_000.0, avg_daily_volume=50_000.0,
        session_volume_multiple=3.0,
    ) is True


def test_should_enter_allows_when_session_volume_clears_the_multiple():
    assert should_enter(
        newly_confirmed_type="resistance_breakout", relative_volume=HIGH_VOLUME,
        volume_confirm_threshold=VOLUME_CONFIRM_THRESHOLD, position_open=False,
        session_cumulative_volume=9_000_000.0, avg_daily_volume=50_000.0,
        session_volume_multiple=3.0,
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


def test_apply_bar_to_open_position_defaults_to_trailing_phase_unaffected_by_feature():
    # A position built without opting into the two-phase feature (every
    # test above this line, and a resumed pre-migration open position,
    # see journal_store.py) must ratchet EXACTLY as always -- exit_phase
    # defaults to "trailing", not "swing_low" (specs.md section 12).
    pos = OpenPosition(id=None, symbol="X", entry_ts=0, entry_price=100.0,
                       high_water_mark=100.0, stop_level=95.0)
    assert pos.exit_phase == "trailing"
    updated, _ = apply_bar_to_open_position(pos, _bar(10, high=110.0, low=105.0, close=108.0))
    assert updated.stop_level == 110.0 * (1 - TRAIL_PCT)
    assert updated.exit_phase == "trailing"


# -- apply_bar_to_open_position: two-phase exit, specs.md section 12 -------

def _swing_low_position(*, entry_price=10.0, trigger_price=9.5,
                        trail_pct=TRAIL_PCT, buffer_pct=SWING_LOW_BUFFER_PCT,
                        progress_pct=PATTERN_PROGRESS_THRESHOLD_PCT,
                        high_water_mark=None):
    hwm = high_water_mark if high_water_mark is not None else entry_price
    return OpenPosition(
        id=1, symbol="AEHL", entry_ts=0, entry_price=entry_price,
        high_water_mark=hwm,
        stop_level=initial_stop_level(trigger_price, buffer_pct),
        factors={"trigger_price": trigger_price},
        trail_pct=trail_pct, exit_phase="swing_low",
        swing_low_buffer_pct_used=buffer_pct,
        pattern_progress_threshold_pct_used=progress_pct,
    )


def test_swing_low_phase_uses_trigger_price_fallback_when_no_swing_low_confirmed():
    pos = _swing_low_position(entry_price=10.0, trigger_price=9.5)
    updated, exit_event = apply_bar_to_open_position(
        pos, _bar(10, high=10.1, low=9.9, close=10.0), swing_low_anchor=None)
    assert updated.stop_level == initial_stop_level(9.5, SWING_LOW_BUFFER_PCT)
    assert exit_event is None
    assert updated.exit_phase == "swing_low"


def test_swing_low_phase_anchors_to_the_confirmed_swing_low_when_present():
    pos = _swing_low_position(entry_price=10.0, trigger_price=9.5)
    updated, _ = apply_bar_to_open_position(
        pos, _bar(10, high=10.1, low=9.9, close=10.0), swing_low_anchor=9.8)
    assert updated.stop_level == initial_stop_level(9.8, SWING_LOW_BUFFER_PCT)


def test_swing_low_phase_a_lower_confirmed_swing_low_lowers_the_stop():
    pos = _swing_low_position(entry_price=10.0, trigger_price=9.5)
    updated, _ = apply_bar_to_open_position(
        pos, _bar(10, high=10.1, low=9.9, close=10.0), swing_low_anchor=9.2)
    assert updated.stop_level == initial_stop_level(9.2, SWING_LOW_BUFFER_PCT)
    assert updated.stop_level < initial_stop_level(9.5, SWING_LOW_BUFFER_PCT)


def test_swing_low_phase_high_water_mark_still_ratchets_even_though_stop_ignores_it():
    # hwm keeps tracking the true peak throughout phase 1 -- needed the
    # moment phase 2 begins, and is how "real progress" is measured.
    pos = _swing_low_position(entry_price=10.0, trigger_price=9.5)
    updated, _ = apply_bar_to_open_position(
        pos, _bar(10, high=10.2, low=10.0, close=10.15), swing_low_anchor=None)
    assert updated.high_water_mark == 10.2
    # stop is still the trigger-price anchor, NOT hwm-derived
    assert updated.stop_level == initial_stop_level(9.5, SWING_LOW_BUFFER_PCT)


def test_swing_low_phase_exit_still_fires_immediately_on_breach():
    pos = _swing_low_position(entry_price=10.0, trigger_price=9.5)
    updated, exit_event = apply_bar_to_open_position(
        pos, _bar(10, high=10.1, low=9.4, close=9.45), swing_low_anchor=None)
    assert exit_event is not None
    assert exit_event.exit_reason == "trailing_stop"
    assert exit_event.exit_price == initial_stop_level(9.5, SWING_LOW_BUFFER_PCT)


def test_phase_transitions_to_trailing_once_progress_threshold_cleared():
    # entry_price=10.0, progress_pct=0.03 -> clears at hwm >= 10.30
    pos = _swing_low_position(entry_price=10.0, trigger_price=9.5, progress_pct=0.03)
    updated, _ = apply_bar_to_open_position(
        pos, _bar(10, high=10.35, low=10.2, close=10.3), swing_low_anchor=None)
    assert updated.exit_phase == "trailing"
    assert updated.phase_transitioned_ts == 10
    # from the moment of transition, the stop is flat-trail-from-hwm
    assert updated.stop_level == 10.35 * (1 - TRAIL_PCT)


def test_phase_stays_swing_low_when_progress_threshold_not_yet_cleared():
    pos = _swing_low_position(entry_price=10.0, trigger_price=9.5, progress_pct=0.03)
    updated, _ = apply_bar_to_open_position(
        pos, _bar(10, high=10.2, low=10.1, close=10.15), swing_low_anchor=None)  # +2%, short of 3%
    assert updated.exit_phase == "swing_low"
    assert updated.phase_transitioned_ts is None


def test_phase_transition_is_one_way_a_later_pullback_does_not_revert_it():
    pos = _swing_low_position(entry_price=10.0, trigger_price=9.5, progress_pct=0.03,
                              high_water_mark=10.5)
    pos = replace(pos, exit_phase="trailing", phase_transitioned_ts=5,
                 stop_level=initial_stop_level(10.5, TRAIL_PCT))
    # A pullback bar -- new high is LOWER than the existing hwm, well
    # below the progress threshold if it were being re-evaluated from
    # here, but the phase must never revert.
    updated, _ = apply_bar_to_open_position(
        pos, _bar(20, high=10.3, low=10.1, close=10.2), swing_low_anchor=8.0)
    assert updated.exit_phase == "trailing"
    assert updated.phase_transitioned_ts == 5  # unchanged, not re-stamped
    # still flat-trail math, completely ignoring the (lower) swing_low_anchor
    assert updated.stop_level == 10.5 * (1 - TRAIL_PCT)


def test_swing_low_phase_falls_back_to_entry_price_when_factors_missing():
    # Defensive: a hand-built position with no factors at all (should
    # never happen via the real entry path, but must not crash).
    pos = OpenPosition(id=1, symbol="AEHL", entry_ts=0, entry_price=10.0,
                       high_water_mark=10.0,
                       stop_level=initial_stop_level(10.0, SWING_LOW_BUFFER_PCT),
                       factors=None, trail_pct=TRAIL_PCT, exit_phase="swing_low",
                       swing_low_buffer_pct_used=SWING_LOW_BUFFER_PCT,
                       pattern_progress_threshold_pct_used=PATTERN_PROGRESS_THRESHOLD_PCT)
    updated, _ = apply_bar_to_open_position(
        pos, _bar(10, high=10.1, low=9.99, close=10.05), swing_low_anchor=None)
    assert updated.stop_level == initial_stop_level(10.0, SWING_LOW_BUFFER_PCT)


def test_swing_low_phase_anchor_is_clamped_to_never_exceed_entry_price():
    # Found live 2026-09-18 (see specs.md): round_number_reclaim's own
    # trigger_price can legitimately sit ABOVE the confirming bar's own
    # close (evaluate_hold never retroactively un-confirms on a later
    # pullback) -- an unclamped anchor there would price phase 1's
    # "protective" stop above the entry itself, a near-guaranteed
    # immediate stop-out. entry_price=10.0, trigger_price=10.8 (ABOVE
    # entry) -- the fallback anchor must clamp to entry_price, not 10.8.
    pos = _swing_low_position(entry_price=10.0, trigger_price=10.8)
    updated, exit_event = apply_bar_to_open_position(
        pos, _bar(10, high=10.1, low=9.99, close=10.05), swing_low_anchor=None)
    assert updated.stop_level == initial_stop_level(10.0, SWING_LOW_BUFFER_PCT)
    assert updated.stop_level < 10.0  # a real, sane floor BELOW entry
    assert exit_event is None


def test_swing_low_phase_confirmed_anchor_above_entry_price_is_also_clamped():
    # Same clamp, the confirmed-swing-low path this time (not just the
    # trigger-price fallback) -- defensive-but-real: a confirmed swing
    # low should rarely exceed entry_price in practice, but the
    # invariant ("phase 1's stop is never above entry") must hold
    # regardless of which anchor source produced the candidate.
    pos = _swing_low_position(entry_price=10.0, trigger_price=9.5)
    updated, _ = apply_bar_to_open_position(
        pos, _bar(10, high=10.1, low=9.99, close=10.05), swing_low_anchor=10.5)
    assert updated.stop_level == initial_stop_level(10.0, SWING_LOW_BUFFER_PCT)


# -- advance_journal: the per-poll orchestration function -------------------

_ALL_FOUR_TYPES = ("resistance_breakout", "micro_breakout",
                   "vwap_reclaim", "round_number_reclaim")


def _advance(*, position=None, new_bars, setups, was_confirmed_types=frozenset(),
            relative_volume=HIGH_VOLUME,
            volume_confirm_threshold=VOLUME_CONFIRM_THRESHOLD, symbol="AEHL",
            trail_pct=TRAIL_PCT, watch_note=None,
            current_equity=CURRENT_EQUITY, risk_pct_per_trade=RISK_PCT_PER_TRADE,
            swing_low_buffer_pct=SWING_LOW_BUFFER_PCT,
            pattern_progress_threshold_pct=PATTERN_PROGRESS_THRESHOLD_PCT,
            swing_low_anchor=None,
            session_cumulative_volume=SESSION_CUMULATIVE_VOLUME,
            avg_daily_volume=None,
            session_volume_multiple=SESSION_VOLUME_MULTIPLE):
    return advance_journal(
        position=position, new_bars=new_bars, setups=setups,
        was_confirmed_types=was_confirmed_types, relative_volume=relative_volume,
        volume_confirm_threshold=volume_confirm_threshold,
        trail_pct=trail_pct, symbol=symbol, watch_note=watch_note,
        current_equity=current_equity, risk_pct_per_trade=risk_pct_per_trade,
        swing_low_buffer_pct=swing_low_buffer_pct,
        pattern_progress_threshold_pct=pattern_progress_threshold_pct,
        swing_low_anchor=swing_low_anchor,
        session_cumulative_volume=session_cumulative_volume,
        avg_daily_volume=avg_daily_volume,
        session_volume_multiple=session_volume_multiple,
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
    # Phase 1 (specs.md section 12): anchored to the entry-trigger level
    # (_setup's default trigger_price=10.5), buffered -- NOT
    # entry_price*(1-trail_pct) anymore, that's phase 2's formula only.
    # Clamped to entry_price (10.2 < trigger_price 10.5, see
    # _phase1_anchor) -- the anchor must never exceed entry_price.
    assert tick.opened.stop_level == 10.2 * (1 - SWING_LOW_BUFFER_PCT)
    assert tick.opened.exit_phase == "swing_low"
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


# -- session-level volume gate, wired through advance_journal (specs.md
# section 12) ----------------------------------------------------------

def test_advance_journal_blocks_entry_when_session_volume_gate_fails():
    tick = _advance(
        new_bars=[_bar(100, high=10.5, low=9.8, close=10.2)],
        setups=[_setup("resistance_breakout", confirmed=True)],
        session_cumulative_volume=1_000.0, avg_daily_volume=1_000_000.0,
        session_volume_multiple=3.0,
    )
    assert tick.opened is None
    # still marked "seen" -- same "doesn't get a second chance while it
    # stays confirmed" treatment as the bar-level volume gate.
    assert tick.confirmed_types_after == {"resistance_breakout"}


def test_advance_journal_allows_entry_when_session_volume_gate_passes():
    tick = _advance(
        new_bars=[_bar(100, high=10.5, low=9.8, close=10.2)],
        setups=[_setup("resistance_breakout", confirmed=True)],
        session_cumulative_volume=5_000_000.0, avg_daily_volume=1_000_000.0,
        session_volume_multiple=3.0,
    )
    assert tick.opened is not None


def test_new_entry_snapshots_session_volume_gate_context():
    tick = _advance(
        new_bars=[_bar(100, high=10.5, low=9.8, close=10.2)],
        setups=[_setup("resistance_breakout", confirmed=True)],
        session_cumulative_volume=5_000_000.0, avg_daily_volume=1_000_000.0,
        session_volume_multiple=3.0,
    )
    assert tick.opened.factors["session_cumulative_volume"] == 5_000_000.0
    assert tick.opened.factors["avg_daily_volume"] == 1_000_000.0
    assert tick.opened.factors["session_volume_multiple_used"] == 3.0


def test_new_entry_snapshots_a_skipped_session_volume_gate_as_none_not_a_fake_pass():
    tick = _advance(
        new_bars=[_bar(100, high=10.5, low=9.8, close=10.2)],
        setups=[_setup("resistance_breakout", confirmed=True)],
        avg_daily_volume=None,  # the "skip the gate" state
    )
    assert tick.opened is not None
    assert tick.opened.factors["avg_daily_volume"] is None


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
    # trail_pct is locked onto the position at entry regardless -- it's
    # simply not what PRICES the entry-time stop_level anymore (that's
    # phase 1's swing-low/trigger-price anchor, specs.md section 12);
    # trail_pct only takes over once the position transitions to phase 2.
    tick = _advance(
        new_bars=[_bar(100, high=10.5, low=9.8, close=10.2)],
        setups=[_setup("resistance_breakout", confirmed=True)],
        trail_pct=0.10,  # NOT the module TRAIL_PCT=0.05 default
    )
    assert tick.opened.trail_pct == 0.10
    # Clamped to entry_price (10.2), same as above -- trigger_price (10.5)
    # exceeds it.
    assert tick.opened.stop_level == 10.2 * (1 - SWING_LOW_BUFFER_PCT)
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


# -- position sizing with compounding virtual equity (specs.md section 7) --

def test_new_entry_computes_shares_from_current_equity_risk_pct_and_trail_pct():
    # risk_amount = 2000 * 0.01 = 20.0; risk_per_share = 10.2 * 0.05 =
    # 0.51; shares = floor(20.0 / 0.51) = 39; risk_amount_used = 39 *
    # 0.51 = 19.89 (the REAL amount risked at this rounded share count,
    # not the theoretical 20.0 target).
    tick = _advance(
        new_bars=[_bar(100, high=10.5, low=9.8, close=10.2)],
        setups=[_setup("resistance_breakout", confirmed=True)],
        current_equity=2000.0, risk_pct_per_trade=0.01, trail_pct=0.05,
    )
    assert tick.opened.shares == 39
    assert tick.opened.account_size_used == 2000.0
    assert tick.opened.risk_pct_used == 0.01
    assert tick.opened.risk_amount_used == pytest.approx(19.89)


def test_new_entry_sizing_reads_current_equity_and_risk_pct_at_the_moment_of_entry():
    # A DIFFERENT current_equity/risk_pct_per_trade than the module
    # defaults -- proves these are read from the live values passed in,
    # not some frozen module-level constant.
    tick = _advance(
        new_bars=[_bar(100, high=10.5, low=9.8, close=10.2)],
        setups=[_setup("resistance_breakout", confirmed=True)],
        current_equity=5000.0, risk_pct_per_trade=0.02, trail_pct=0.05,
    )
    assert tick.opened.account_size_used == 5000.0
    assert tick.opened.risk_pct_used == 0.02
    # risk_amount = 100.0; risk_per_share = 10.2*0.05 = 0.51; shares =
    # floor(100.0/0.51) = 196
    assert tick.opened.shares == 196


def test_new_entry_zero_shares_when_risk_amount_is_smaller_than_one_share():
    # A tiny current_equity (or tight risk_pct) relative to the stock's
    # own price/stop distance -- shares rounds DOWN to 0, a real, valid,
    # journaled outcome (specs.md: "the trade still logs ... but visibly
    # flagged as zero-size"), never a crash or a negative/fractional count.
    tick = _advance(
        new_bars=[_bar(100, high=10.5, low=9.8, close=10.2)],
        setups=[_setup("resistance_breakout", confirmed=True)],
        current_equity=1.0, risk_pct_per_trade=0.01, trail_pct=0.05,
    )
    assert tick.opened.shares == 0
    assert tick.opened.shares is not None
    assert tick.opened.risk_amount_used == 0.0
    assert tick.opened.account_size_used == 1.0
    assert tick.opened.risk_pct_used == 0.01


def test_open_positions_ratcheting_does_not_touch_sizing_fields():
    # Sizing is an entry-time-only concern -- apply_bar_to_open_position
    # (via advance_journal's ratchet loop) must never recompute or clear
    # it on a position that's simply continuing.
    pos = OpenPosition(id=1, symbol="AEHL", entry_ts=0, entry_price=10.0,
                       high_water_mark=10.0, stop_level=9.5,
                       shares=39, account_size_used=2000.0,
                       risk_pct_used=0.01, risk_amount_used=19.89)
    tick = _advance(
        position=pos,
        new_bars=[_bar(10, high=11.0, low=10.6, close=10.9)],
        setups=[], current_equity=99999.0,  # a different live value, must be ignored
    )
    assert tick.updated.shares == 39
    assert tick.updated.account_size_used == 2000.0
    assert tick.updated.risk_pct_used == 0.01
    assert tick.updated.risk_amount_used == 19.89


# -- two-phase exit snapshot at entry, specs.md section 12 -----------------

def test_new_entry_starts_in_swing_low_phase_with_thresholds_locked_in():
    tick = _advance(
        new_bars=[_bar(100, high=10.5, low=9.8, close=10.2)],
        setups=[_setup("resistance_breakout", confirmed=True)],
        swing_low_buffer_pct=0.008, pattern_progress_threshold_pct=0.04,
    )
    assert tick.opened.exit_phase == "swing_low"
    assert tick.opened.swing_low_buffer_pct_used == 0.008
    assert tick.opened.pattern_progress_threshold_pct_used == 0.04
    assert tick.opened.phase_transitioned_ts is None


def test_new_entry_stop_level_is_clamped_when_trigger_price_exceeds_entry_price():
    # Same real scenario as test_swing_low_phase_anchor_is_clamped_..._
    # entry_price, at entry-construction time this time -- confirmed
    # directly against real setup_types.evaluate_setups output (see
    # specs.md): round_number_reclaim can confirm with trigger_price
    # above the confirming bar's own close.
    tick = _advance(
        new_bars=[_bar(100, high=10.5, low=9.8, close=9.1)],
        setups=[_setup("round_number_reclaim", confirmed=True, trigger_price=9.25)],
    )
    assert tick.opened.entry_price == 9.1
    assert tick.opened.stop_level == initial_stop_level(9.1, SWING_LOW_BUFFER_PCT)
    assert tick.opened.stop_level < tick.opened.entry_price


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


def test_advance_journal_reopen_in_the_same_batch_sizes_off_post_close_equity():
    # Found live 2026-09-18, caught with fresh instrumented evidence
    # against a real cascading replay (see specs.md): app.py's
    # _update_journal reads current_equity ONCE, before calling
    # advance_journal -- but this SAME call can both close the existing
    # position (test_advance_journal_can_both_close_and_reopen_within_
    # one_batch, above) and size a fresh reopen, and the close's real
    # dollar P&L only reaches current_equity in storage AFTER
    # advance_journal returns (app.py applies it in the tick.closed
    # branch, which runs after tick.opened was already computed). A
    # same-batch reopen must therefore size off current_equity ADJUSTED
    # for the just-closed position's own realized P&L -- pure
    # arithmetic already available here (the closing position's own
    # shares/entry_price, and the exit event's exit_price), not the
    # raw pre-close value passed in from outside.
    entry_price = 10.0
    shares_closing = 100
    pos = OpenPosition(id=1, symbol="AEHL", entry_ts=0, entry_price=entry_price,
                       high_water_mark=entry_price,
                       stop_level=initial_stop_level(entry_price, TRAIL_PCT),
                       shares=shares_closing, trail_pct=TRAIL_PCT)
    bars = [
        _bar(10, high=10.2, low=9.4, close=9.45),    # breaches, closes it
        _bar(20, high=10.6, low=10.5, close=10.55),  # fresh confirmation bar
    ]
    tick = _advance(
        position=pos, new_bars=bars,
        setups=[_setup("round_number_reclaim", confirmed=True, distance=0.1)],
        current_equity=2000.0, risk_pct_per_trade=0.01, trail_pct=TRAIL_PCT,
    )
    assert tick.closed is not None
    closed_position, exit_event = tick.closed
    closing_pnl_dollars = closed_position.shares * (exit_event.exit_price - closed_position.entry_price)
    assert closing_pnl_dollars != 0.0  # a real, nonzero closing P&L for this scenario

    assert tick.opened is not None
    expected_effective_equity = 2000.0 + closing_pnl_dollars
    assert tick.opened.account_size_used == pytest.approx(expected_effective_equity)
    # ... which is NOT the raw 2000.0 that was passed in -- the exact bug.
    assert tick.opened.account_size_used != 2000.0

    risk_amount = expected_effective_equity * 0.01
    risk_per_share = tick.opened.entry_price * TRAIL_PCT
    assert tick.opened.shares == math.floor(risk_amount / risk_per_share)
    assert tick.opened.risk_amount_used == pytest.approx(tick.opened.shares * risk_per_share)


def test_advance_journal_no_bars_is_a_safe_noop():
    tick = _advance(new_bars=[], setups=[])
    assert tick.opened is None
    assert tick.updated is None
    assert tick.closed is None
