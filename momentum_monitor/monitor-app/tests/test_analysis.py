import os
import sys

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _APP_DIR)

from analysis import (
    MIN_BUCKET_SIZE,
    MIN_TRADES_FOR_STATS,
    REAL_TRADE_EXIT_REASONS,
    breakdown_by_review_label,
    breakdown_by_setup_type,
    losses_section,
    overall_stats,
    real_trades,
)

RTH = 1756909800  # 2025-09-03 10:30:00 ET


def _trade(symbol="AEHL", exit_reason="trailing_stop", realized_pnl_pct=1.0,
          realized_pnl_dollars=None, setup_type="resistance_breakout",
          review_label=None, entry_ts=RTH, trade_id=1):
    return {
        "id": trade_id, "symbol": symbol, "exit_reason": exit_reason,
        "realized_pnl_pct": realized_pnl_pct,
        "realized_pnl_dollars": realized_pnl_dollars,
        "setup_type": setup_type, "review_label": review_label,
        "entry_ts": entry_ts,
    }


# -- real_trades: the ONE filtering function every stat below reuses ------

def test_real_trades_excludes_symbol_switched():
    closed = [
        _trade(trade_id=1, exit_reason="trailing_stop", realized_pnl_pct=5.0),
        _trade(trade_id=2, exit_reason="symbol_switched", realized_pnl_pct=-50.0),
    ]
    result = real_trades(closed)
    assert [t["id"] for t in result] == [1]


def test_real_trades_on_a_mixed_realistic_dataset():
    # A real-shaped mixed-reason dataset -- most of a project's actual
    # accumulated history (per specs.md section 6) IS symbol_switched
    # housekeeping noise, not real trailing_stop outcomes.
    closed = [
        _trade(trade_id=1, exit_reason="trailing_stop", realized_pnl_pct=8.2),
        _trade(trade_id=2, exit_reason="symbol_switched", realized_pnl_pct=-2.1),
        _trade(trade_id=3, exit_reason="trailing_stop", realized_pnl_pct=-4.5),
        _trade(trade_id=4, exit_reason="symbol_switched", realized_pnl_pct=15.0),
        _trade(trade_id=5, exit_reason="trailing_stop", realized_pnl_pct=2.0),
    ]
    result = real_trades(closed)
    assert [t["id"] for t in result] == [1, 3, 5]


def test_real_trade_exit_reasons_is_trailing_stop_only_for_now():
    # Documents the current real set explicitly -- a future target_hit
    # exit reason would join this, symbol_switched never will.
    assert REAL_TRADE_EXIT_REASONS == frozenset({"trailing_stop"})


# -- overall_stats ----------------------------------------------------------

def test_overall_stats_computes_win_rate_and_expectancy_correctly_filtered():
    closed = [_trade(trade_id=i, realized_pnl_pct=pct) for i, pct in
             enumerate([10.0, -5.0, 3.0, -2.0, 8.0, -1.0, 6.0, 4.0, -3.0, 2.0], start=1)]
    closed.append(_trade(trade_id=99, exit_reason="symbol_switched", realized_pnl_pct=1000.0))
    stats = overall_stats(closed)
    assert stats["count"] == 10  # the symbol_switched row excluded
    assert stats["wins"] == 6
    assert stats["losses"] == 4
    assert stats["win_rate"] == 0.6
    assert stats["expectancy_pct"] == sum(
        [10.0, -5.0, 3.0, -2.0, 8.0, -1.0, 6.0, 4.0, -3.0, 2.0]) / 10
    assert stats["sufficient_sample"] is True
    assert stats["note"] is None  # no caveat needed once sufficient


def test_overall_stats_breakeven_trade_counted_separately_from_win_or_loss():
    closed = [_trade(trade_id=1, realized_pnl_pct=0.0),
             _trade(trade_id=2, realized_pnl_pct=5.0)]
    stats = overall_stats(closed)
    assert stats["wins"] == 1
    assert stats["losses"] == 0
    assert stats["breakeven"] == 1
    assert stats["count"] == 2


def test_overall_stats_flags_insufficient_sample_explicitly():
    closed = [_trade(trade_id=1, realized_pnl_pct=5.0),
             _trade(trade_id=2, realized_pnl_pct=-3.0)]
    stats = overall_stats(closed)
    assert stats["count"] == 2
    assert stats["sufficient_sample"] is False
    assert stats["note"] is not None
    assert "2" in stats["note"]
    assert str(MIN_TRADES_FOR_STATS) in stats["note"]


def test_overall_stats_on_zero_real_trades_says_so_not_a_divide_by_zero():
    closed = [_trade(trade_id=1, exit_reason="symbol_switched", realized_pnl_pct=5.0)]
    stats = overall_stats(closed)
    assert stats["count"] == 0
    assert stats["win_rate"] is None
    assert stats["expectancy_pct"] is None
    assert stats["note"] is not None


# -- breakdown_by_setup_type -------------------------------------------------

def test_breakdown_by_setup_type_groups_correctly():
    closed = (
        [_trade(trade_id=i, setup_type="resistance_breakout", realized_pnl_pct=p)
         for i, p in enumerate([5.0, -2.0, 3.0], start=1)]
        + [_trade(trade_id=i, setup_type="vwap_reclaim", realized_pnl_pct=p)
           for i, p in enumerate([-1.0, -4.0], start=10)]
    )
    breakdown = breakdown_by_setup_type(closed)
    assert breakdown["resistance_breakout"]["count"] == 3
    assert breakdown["resistance_breakout"]["wins"] == 2
    assert breakdown["vwap_reclaim"]["count"] == 2
    assert breakdown["vwap_reclaim"]["losses"] == 2


def test_breakdown_by_setup_type_excludes_symbol_switched_rows():
    closed = [
        _trade(trade_id=1, setup_type="resistance_breakout", realized_pnl_pct=5.0),
        _trade(trade_id=2, setup_type="resistance_breakout",
              exit_reason="symbol_switched", realized_pnl_pct=-99.0),
    ]
    breakdown = breakdown_by_setup_type(closed)
    assert breakdown["resistance_breakout"]["count"] == 1


def test_breakdown_by_setup_type_buckets_missing_type_as_unknown():
    closed = [_trade(trade_id=1, setup_type=None, realized_pnl_pct=5.0)]
    breakdown = breakdown_by_setup_type(closed)
    assert breakdown["unknown"]["count"] == 1


def test_breakdown_by_setup_type_each_group_flags_its_own_sufficiency():
    closed = [_trade(trade_id=1, setup_type="resistance_breakout", realized_pnl_pct=5.0)]
    breakdown = breakdown_by_setup_type(closed)
    assert breakdown["resistance_breakout"]["sufficient_sample"] is False
    assert breakdown["resistance_breakout"]["note"] is not None


# -- breakdown_by_review_label -----------------------------------------------

def test_breakdown_by_review_label_only_counts_reviewed_trades():
    closed = [
        _trade(trade_id=1, review_label="clean_signal", realized_pnl_pct=5.0),
        _trade(trade_id=2, review_label=None, realized_pnl_pct=3.0),  # not reviewed
        _trade(trade_id=3, review_label="bad_signal", realized_pnl_pct=-4.0),
    ]
    result = breakdown_by_review_label(closed)
    assert result["total_real_count"] == 3
    assert result["reviewed_count"] == 2
    assert set(result["breakdown"]) == {"clean_signal", "bad_signal"}
    assert result["breakdown"]["clean_signal"]["count"] == 1
    assert result["breakdown"]["bad_signal"]["count"] == 1


def test_breakdown_by_review_label_excludes_symbol_switched_rows():
    closed = [
        _trade(trade_id=1, review_label="clean_signal", realized_pnl_pct=5.0),
        _trade(trade_id=2, review_label="clean_signal", exit_reason="symbol_switched",
              realized_pnl_pct=-99.0),
    ]
    result = breakdown_by_review_label(closed)
    assert result["breakdown"]["clean_signal"]["count"] == 1


def test_breakdown_by_review_label_flags_insufficient_reviewed_sample():
    closed = [_trade(trade_id=1, review_label="clean_signal", realized_pnl_pct=5.0)]
    result = breakdown_by_review_label(closed)
    assert result["note"] is not None  # only 1 reviewed trade overall


def test_breakdown_by_review_label_bad_signal_correlates_with_losses_once_meaningful():
    # The actual point of the feature (specs.md section 26): does
    # bad_signal correlate with losses, and can clean_signal still lose
    # sometimes (expected and healthy)?
    closed = (
        [_trade(trade_id=i, review_label="bad_signal", realized_pnl_pct=p)
         for i, p in enumerate([-5.0, -3.0, -8.0, -1.0, 2.0, -6.0, -2.0, -4.0, -1.5, -3.5],
                               start=1)]
        + [_trade(trade_id=i, review_label="clean_signal", realized_pnl_pct=p)
           for i, p in enumerate([5.0, 4.0, -2.0, 6.0, 3.0, 7.0, -1.0, 5.5, 4.5, 6.5],
                                 start=100)]
    )
    result = breakdown_by_review_label(closed)
    bad = result["breakdown"]["bad_signal"]
    clean = result["breakdown"]["clean_signal"]
    assert bad["sufficient_sample"] is True
    assert clean["sufficient_sample"] is True
    assert bad["win_rate"] < clean["win_rate"]  # validates the label means something
    assert clean["losses"] >= 1  # a clean signal can still lose -- expected, not a red flag


# -- losses_section -----------------------------------------------------------

def test_losses_section_computes_average_loss_size():
    closed = [
        _trade(trade_id=1, realized_pnl_pct=5.0),
        _trade(trade_id=2, realized_pnl_pct=-4.0, realized_pnl_dollars=-40.0),
        _trade(trade_id=3, realized_pnl_pct=-8.0, realized_pnl_dollars=-80.0),
    ]
    section = losses_section(closed)
    assert section["count"] == 2
    assert section["avg_loss_pct"] == -6.0
    assert section["avg_loss_dollars"] == -60.0


def test_losses_section_handles_missing_dollar_amounts_honestly():
    # A pre-migration row with no realized_pnl_dollars at all -- must not
    # silently treat it as a $0 loss.
    closed = [_trade(trade_id=1, realized_pnl_pct=-4.0, realized_pnl_dollars=None)]
    section = losses_section(closed)
    assert section["avg_loss_dollars"] is None


def test_losses_section_excludes_symbol_switched_from_everything():
    closed = [
        _trade(trade_id=1, realized_pnl_pct=-100.0, exit_reason="symbol_switched"),
        _trade(trade_id=2, realized_pnl_pct=-4.0),
    ]
    section = losses_section(closed)
    assert section["count"] == 1
    assert section["total_real_count"] == 1


def test_losses_section_setup_type_distribution_among_losses():
    closed = [
        _trade(trade_id=1, setup_type="resistance_breakout", realized_pnl_pct=-5.0),
        _trade(trade_id=2, setup_type="resistance_breakout", realized_pnl_pct=-3.0),
        _trade(trade_id=3, setup_type="vwap_reclaim", realized_pnl_pct=-1.0),
        _trade(trade_id=4, setup_type="resistance_breakout", realized_pnl_pct=6.0),  # a win
    ]
    section = losses_section(closed)
    by_type = section["by_setup_type"]
    assert by_type["resistance_breakout"]["trades"] == 3
    assert by_type["resistance_breakout"]["losses"] == 2
    assert by_type["vwap_reclaim"]["trades"] == 1
    assert by_type["vwap_reclaim"]["losses"] == 1


def test_losses_section_review_label_distribution_among_losses():
    closed = [
        _trade(trade_id=1, review_label="bad_signal", realized_pnl_pct=-5.0),
        _trade(trade_id=2, review_label="clean_signal", realized_pnl_pct=-1.0),
        _trade(trade_id=3, review_label="clean_signal", realized_pnl_pct=4.0),
    ]
    section = losses_section(closed)
    by_label = section["by_review_label"]
    assert by_label["bad_signal"]["losses"] == 1
    assert by_label["clean_signal"]["losses"] == 1
    assert by_label["clean_signal"]["trades"] == 2


def test_losses_section_clusters_by_symbol():
    closed = (
        [_trade(trade_id=i, symbol="AEHL", realized_pnl_pct=p)
         for i, p in enumerate([-5.0, -3.0, -8.0, 5.0], start=1)]
        + [_trade(trade_id=i, symbol="MSFT", realized_pnl_pct=p)
           for i, p in enumerate([4.0, 3.0, 6.0, -2.0], start=10)]
    )
    section = losses_section(closed)
    by_symbol = section["by_symbol"]
    assert by_symbol["AEHL"]["losses"] == 3
    assert by_symbol["AEHL"]["trades"] == 4
    assert by_symbol["MSFT"]["losses"] == 1
    assert by_symbol["MSFT"]["trades"] == 4


def test_losses_section_flags_a_bucket_with_an_elevated_loss_rate():
    # AEHL loses 3/4 (75%); MSFT loses 1/4 (25%) -- overall 4/8 (50%).
    # AEHL's own rate is meaningfully above the overall rate and has
    # enough trades in it to say so.
    closed = (
        [_trade(trade_id=i, symbol="AEHL", realized_pnl_pct=p)
         for i, p in enumerate([-5.0, -3.0, -8.0, 5.0], start=1)]
        + [_trade(trade_id=i, symbol="MSFT", realized_pnl_pct=p)
           for i, p in enumerate([4.0, 3.0, 6.0, -2.0], start=10)]
    )
    section = losses_section(closed)
    assert section["overall_loss_rate"] == 0.5
    assert section["by_symbol"]["AEHL"]["loss_rate"] == 0.75
    assert section["by_symbol"]["AEHL"]["elevated_vs_overall"] is True
    assert section["by_symbol"]["MSFT"]["elevated_vs_overall"] is False


def test_losses_section_does_not_flag_elevated_below_the_bucket_sample_floor():
    # A single trade for a rarely-seen symbol that happens to be a loss
    # is 100% loss rate but MEANS NOTHING with n=1 -- must not be flagged
    # as "elevated," which would misrepresent a coin flip as a pattern.
    closed = (
        [_trade(trade_id=i, symbol="AEHL", realized_pnl_pct=p)
         for i, p in enumerate([5.0, 4.0, 3.0, 6.0], start=1)]
        + [_trade(trade_id=99, symbol="RARE", realized_pnl_pct=-1.0)]
    )
    section = losses_section(closed)
    assert section["by_symbol"]["RARE"]["loss_rate"] == 1.0
    assert section["by_symbol"]["RARE"]["sufficient_sample"] is False
    assert section["by_symbol"]["RARE"]["elevated_vs_overall"] is False
    assert MIN_BUCKET_SIZE > 1  # sanity: the floor is actually above this bucket's size


def test_losses_section_clusters_by_hour_of_day():
    # RTH = 2025-09-03 10:30:00 ET -- entries an hour later land in the
    # 11:00 ET hour bucket.
    closed = [
        _trade(trade_id=1, entry_ts=RTH, realized_pnl_pct=-5.0),          # 10:30 ET -> hour 10
        _trade(trade_id=2, entry_ts=RTH + 3600, realized_pnl_pct=4.0),    # 11:30 ET -> hour 11
    ]
    section = losses_section(closed)
    by_hour = section["by_hour_of_day"]
    assert by_hour[10]["losses"] == 1
    assert by_hour[11]["losses"] == 0


def test_losses_section_on_zero_losses_reports_cleanly():
    closed = [_trade(trade_id=1, realized_pnl_pct=5.0)]
    section = losses_section(closed)
    assert section["count"] == 0
    assert section["avg_loss_pct"] is None
    assert section["avg_loss_dollars"] is None
