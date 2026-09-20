import os
import sys

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _APP_DIR)

from journal_logic import ExitEvent, OpenPosition
from narration import (
    breaker_should_trip,
    confirmation_prompt,
    entry_prompt,
    exit_prompt,
    is_armed,
    newly_confirmed_types,
    prune_and_record_call,
)


# -- trigger 1: newly_confirmed_types (reuses journal_logic's own -------
# confirmed_types_after/was_confirmed_types bookkeeping, no new detection)

def test_newly_confirmed_types_is_the_set_difference():
    after = frozenset({"resistance_breakout", "micro_breakout"})
    before = frozenset({"micro_breakout"})
    assert newly_confirmed_types(after, before) == frozenset({"resistance_breakout"})


def test_newly_confirmed_types_empty_when_nothing_new():
    same = frozenset({"resistance_breakout"})
    assert newly_confirmed_types(same, same) == frozenset()


def test_newly_confirmed_types_all_new_on_first_ever_tick():
    after = frozenset({"vwap_reclaim", "round_number_reclaim"})
    before = frozenset()
    assert newly_confirmed_types(after, before) == after


# -- prompts: simple, factual, additive commentary -- not detection ----

def test_confirmation_prompt_includes_symbol_type_and_trigger_price():
    setup = {"trigger_price": 11.45, "distance": 0.32}
    prompt = confirmation_prompt("AEHL", "resistance_breakout", setup)
    assert "AEHL" in prompt
    assert "11.45" in prompt
    assert "resistance breakout" in prompt.lower()


def test_entry_prompt_includes_symbol_setup_type_price_and_shares():
    position = OpenPosition(id=1, symbol="AEHL", entry_ts=100, entry_price=10.2,
                            high_water_mark=10.2, stop_level=9.7,
                            setup_type="micro_breakout", shares=39)
    prompt = entry_prompt("AEHL", position)
    assert "AEHL" in prompt
    assert "10.2" in prompt
    assert "39" in prompt
    assert "micro breakout" in prompt.lower()


def test_entry_prompt_handles_shares_never_computed():
    position = OpenPosition(id=1, symbol="AEHL", entry_ts=100, entry_price=10.2,
                            high_water_mark=10.2, stop_level=9.7,
                            setup_type="micro_breakout", shares=None)
    prompt = entry_prompt("AEHL", position)
    assert "not computed" in prompt.lower()


def test_exit_prompt_includes_pnl_percent_and_dollars():
    position = OpenPosition(id=1, symbol="AEHL", entry_ts=100, entry_price=10.0,
                            high_water_mark=11.0, stop_level=10.45,
                            setup_type="resistance_breakout")
    exit_event = ExitEvent(exit_ts=200, exit_price=10.9, exit_reason="trailing_stop")
    prompt = exit_prompt("AEHL", position, exit_event, pnl_pct=9.0, pnl_dollars=35.1)
    assert "AEHL" in prompt
    assert "+9.00%" in prompt
    assert "+35.10" in prompt
    assert "trailing stop" in prompt.lower()


def test_exit_prompt_handles_a_loss_with_correct_sign():
    position = OpenPosition(id=1, symbol="AEHL", entry_ts=100, entry_price=10.0,
                            high_water_mark=10.0, stop_level=9.5,
                            setup_type="resistance_breakout")
    exit_event = ExitEvent(exit_ts=200, exit_price=9.4, exit_reason="trailing_stop")
    prompt = exit_prompt("AEHL", position, exit_event, pnl_pct=-6.0, pnl_dollars=-23.4)
    assert "-6.00%" in prompt
    assert "-23.40" in prompt


def test_exit_prompt_handles_pnl_dollars_never_computed():
    position = OpenPosition(id=1, symbol="AEHL", entry_ts=100, entry_price=10.0,
                            high_water_mark=10.0, stop_level=9.5,
                            setup_type="resistance_breakout")
    exit_event = ExitEvent(exit_ts=200, exit_price=9.4, exit_reason="trailing_stop")
    prompt = exit_prompt("AEHL", position, exit_event, pnl_pct=-6.0, pnl_dollars=None)
    assert "-6.00%" in prompt
    assert "$" not in prompt


# -- safety gate 2: mandatory hourly re-arm -----------------------------

def test_is_armed_true_when_now_before_armed_until():
    assert is_armed(armed_until=1000.0, now=999.0) is True


def test_is_armed_false_once_expired():
    assert is_armed(armed_until=1000.0, now=1000.1) is False


def test_is_armed_false_when_never_armed():
    assert is_armed(armed_until=None, now=1000.0) is False


# -- safety gate 1: rate-limit circuit breaker --------------------------

def test_prune_and_record_call_drops_timestamps_outside_the_window():
    # window = 10 minutes = 600s; now = 1000
    existing = [100.0, 399.0, 401.0, 900.0]  # 100/399 are >600s old, dropped
    result = prune_and_record_call(existing, now=1000.0, window_minutes=10.0)
    assert result == [401.0, 900.0, 1000.0]


def test_prune_and_record_call_keeps_everything_inside_the_window():
    existing = [950.0, 980.0]
    result = prune_and_record_call(existing, now=1000.0, window_minutes=10.0)
    assert result == [950.0, 980.0, 1000.0]


def test_breaker_should_trip_true_once_over_the_threshold():
    assert breaker_should_trip([1.0, 2.0, 3.0], max_calls_per_window=2.0) is True


def test_breaker_should_trip_false_at_exactly_the_threshold():
    # "MORE than" the threshold trips it -- exactly at the threshold does not.
    assert breaker_should_trip([1.0, 2.0], max_calls_per_window=2.0) is False


def test_breaker_should_trip_false_well_under_the_threshold():
    assert breaker_should_trip([1.0], max_calls_per_window=10.0) is False
