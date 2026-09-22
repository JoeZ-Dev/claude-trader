import os
import sys

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _APP_DIR)

from pattern_flags import (
    DEFAULT_FLAG_COUNT_THRESHOLD,
    FLAG_LABELS,
    compute_flags,
    meets_flag_threshold,
    pattern_flag_prompt,
)


# -- compute_flags: each of the six factors, independently -----------------
# (specs.md section 37: five of the six are named for the DISAGREEING
# condition itself -- macd_negative, below_vwap, below_day_open,
# ema_misaligned, no_news all mean TRUE = a bearish/disagreeing signal.
# volume_not_confirmed is named to match that same polarity, even though
# the task's own shorthand called it "volume_confirmed" -- see the
# module docstring for why.)

def _kwargs(**overrides):
    base = dict(
        price=10.0, macd=0.01, vwap=9.5, day_open=9.0, ema9=10.1, ema20=10.0,
        watch_note="real catalyst", relative_volume=2.0,
        volume_confirm_threshold=1.5,
    )
    base.update(overrides)
    return base


def test_compute_flags_all_clear_on_a_genuinely_clean_confirmation():
    flags = compute_flags(**_kwargs())
    assert flags["volume_not_confirmed"] is False
    assert flags["macd_negative"] is False
    assert flags["below_vwap"] is False
    assert flags["below_day_open"] is False
    assert flags["ema_misaligned"] is False
    assert flags["no_news"] is False
    assert flags["flag_count"] == 0


def test_compute_flags_volume_not_confirmed_reuses_the_real_gate():
    # Bar-level gate fails -- reuses journal_logic.volume_gate_clears,
    # not a reimplementation.
    flags = compute_flags(**_kwargs(relative_volume=1.0, volume_confirm_threshold=1.5))
    assert flags["volume_not_confirmed"] is True


def test_compute_flags_volume_not_confirmed_also_checks_session_gate():
    flags = compute_flags(**_kwargs(
        session_cumulative_volume=100_000.0, avg_daily_volume=50_000.0,
        session_volume_multiple=3.0,  # needs >= 150,000
    ))
    assert flags["volume_not_confirmed"] is True


def test_compute_flags_macd_negative():
    flags = compute_flags(**_kwargs(macd=-0.01))
    assert flags["macd_negative"] is True
    flags_zero = compute_flags(**_kwargs(macd=0.0))
    assert flags_zero["macd_negative"] is False  # exactly zero is not negative


def test_compute_flags_below_vwap():
    flags = compute_flags(**_kwargs(price=9.0, vwap=9.5))
    assert flags["below_vwap"] is True


def test_compute_flags_below_vwap_false_when_vwap_unknown():
    # vwap can genuinely be None (specs.md state.py) -- never crashes,
    # never silently flags a disagreement from missing data.
    flags = compute_flags(**_kwargs(vwap=None))
    assert flags["below_vwap"] is False


def test_compute_flags_below_day_open():
    flags = compute_flags(**_kwargs(price=8.9, day_open=9.0))
    assert flags["below_day_open"] is True


def test_compute_flags_below_day_open_false_when_day_open_unknown():
    flags = compute_flags(**_kwargs(day_open=None))
    assert flags["below_day_open"] is False


def test_compute_flags_ema_misaligned():
    flags = compute_flags(**_kwargs(ema9=9.9, ema20=10.0))
    assert flags["ema_misaligned"] is True


def test_compute_flags_no_news_when_watch_note_empty_or_none():
    assert compute_flags(**_kwargs(watch_note=None))["no_news"] is True
    assert compute_flags(**_kwargs(watch_note=""))["no_news"] is True
    assert compute_flags(**_kwargs(watch_note="real catalyst"))["no_news"] is False


def test_compute_flags_counts_multiple_disagreements():
    flags = compute_flags(**_kwargs(
        macd=-0.01, price=8.9, vwap=9.5, day_open=9.0, watch_note=None,
    ))
    assert flags["macd_negative"] is True
    assert flags["below_vwap"] is True
    assert flags["below_day_open"] is True
    assert flags["no_news"] is True
    assert flags["flag_count"] == 4


# -- meets_flag_threshold ----------------------------------------------------

def test_meets_flag_threshold_default_is_2():
    assert DEFAULT_FLAG_COUNT_THRESHOLD == 2


def test_meets_flag_threshold_fires_at_and_above_threshold():
    assert meets_flag_threshold(2, threshold=2) is True
    assert meets_flag_threshold(3, threshold=2) is True


def test_meets_flag_threshold_stays_silent_below_threshold():
    assert meets_flag_threshold(0, threshold=2) is False
    assert meets_flag_threshold(1, threshold=2) is False


# -- pattern_flag_prompt: real numbers, real context, no fabrication -------

def test_pattern_flag_prompt_names_only_the_active_flags():
    flags = compute_flags(**_kwargs(macd=-0.01, price=8.9, vwap=9.5, day_open=9.0))
    prompt = pattern_flag_prompt("AEHL", "resistance_breakout", 10.5, 0.5, flags)
    assert "AEHL" in prompt
    assert "resistance breakout" in prompt
    assert "10.5" in prompt
    assert FLAG_LABELS["macd_negative"] in prompt
    assert FLAG_LABELS["below_vwap"] in prompt
    assert FLAG_LABELS["below_day_open"] in prompt
    # inactive flags must not appear
    assert FLAG_LABELS["no_news"] not in prompt
    assert FLAG_LABELS["ema_misaligned"] not in prompt


def test_pattern_flag_prompt_includes_the_real_flag_count():
    flags = compute_flags(**_kwargs(macd=-0.01, watch_note=None))
    assert flags["flag_count"] == 2
    prompt = pattern_flag_prompt("AEHL", "vwap_reclaim", 10.5, 0.5, flags)
    assert "2" in prompt
