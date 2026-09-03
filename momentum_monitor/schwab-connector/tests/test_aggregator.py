import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aggregator import BarAggregator, bucket_start, is_extended_hours

# Fixed epochs (no wall-clock dependence). America/New_York, September 2025 = EDT (UTC-4).
RTH_1030 = 1756909800        # 2025-09-03 (Wed) 10:30:00 ET -> regular hours
PREMARKET_0800 = 1756900800  # 2025-09-03 (Wed) 08:00:00 ET -> extended hours

BAR_KEYS = {"ts", "open", "high", "low", "close", "volume", "is_extended"}


def tick(ts, price, size):
    return {"ts": ts, "price": price, "size": size}


def test_bucket_start_floors_to_10s_grid():
    assert bucket_start(RTH_1030 + 3) == RTH_1030
    assert bucket_start(RTH_1030 + 9.999) == RTH_1030
    assert bucket_start(RTH_1030 + 10) == RTH_1030 + 10


def test_is_extended_hours_rth_vs_premarket_vs_weekend():
    assert is_extended_hours(RTH_1030) is False
    assert is_extended_hours(PREMARKET_0800) is True
    # 2025-09-06 is a Saturday; 10:30 ET that day must read as extended.
    saturday_1030 = RTH_1030 + 3 * 86400
    assert is_extended_hours(saturday_1030) is True


def test_single_bucket_ohlcv():
    agg = BarAggregator()
    agg.feed(tick(RTH_1030 + 1, 10.0, 100))
    agg.feed(tick(RTH_1030 + 4, 10.5, 50))
    agg.feed(tick(RTH_1030 + 7, 9.8, 25))
    agg.flush(RTH_1030 + 10)  # move past the bucket so it finalizes
    bars = agg.drain()
    assert len(bars) == 1
    b = bars[0]
    assert b["ts"] == RTH_1030
    assert b["open"] == 10.0
    assert b["high"] == 10.5
    assert b["low"] == 9.8
    assert b["close"] == 9.8
    assert b["volume"] == 175
    assert b["is_extended"] is False


def test_bar_shape_keys_match_contract():
    agg = BarAggregator()
    agg.feed(tick(RTH_1030 + 1, 10.0, 100))
    agg.flush(RTH_1030 + 10)
    (b,) = agg.drain()
    assert set(b.keys()) == BAR_KEYS


def test_bucket_rollover_finalizes_previous_without_flush():
    agg = BarAggregator()
    agg.feed(tick(RTH_1030 + 2, 10.0, 100))
    agg.feed(tick(RTH_1030 + 8, 10.2, 100))
    # A tick in the NEXT bucket should finalize the first bucket immediately.
    agg.feed(tick(RTH_1030 + 12, 10.3, 100))
    bars = agg.drain()
    assert len(bars) == 1
    assert bars[0]["ts"] == RTH_1030
    assert bars[0]["close"] == 10.2


def test_empty_buckets_are_forward_filled_flat_zero_volume():
    agg = BarAggregator()
    agg.feed(tick(RTH_1030 + 3, 10.0, 100))       # bucket 0
    agg.feed(tick(RTH_1030 + 33, 11.0, 100))      # bucket 3 -> buckets 1 and 2 skipped
    bars = agg.drain()
    # bucket 0 real, buckets 1 and 2 forward-filled
    assert [b["ts"] for b in bars] == [RTH_1030, RTH_1030 + 10, RTH_1030 + 20]
    for fill in bars[1:]:
        assert fill["open"] == fill["high"] == fill["low"] == fill["close"] == 10.0
        assert fill["volume"] == 0.0


def test_flush_forward_fills_quiet_tail():
    agg = BarAggregator()
    agg.feed(tick(RTH_1030 + 3, 10.0, 100))
    agg.flush(RTH_1030 + 35)  # 3 buckets later, no ticks since
    bars = agg.drain()
    assert [b["ts"] for b in bars] == [RTH_1030, RTH_1030 + 10, RTH_1030 + 20]
    assert bars[-1]["close"] == 10.0
    assert bars[-1]["volume"] == 0.0


def test_out_of_order_tick_is_dropped():
    agg = BarAggregator()
    agg.feed(tick(RTH_1030 + 12, 10.0, 100))   # opens bucket 1
    agg.feed(tick(RTH_1030 + 3, 9.0, 100))     # late tick for bucket 0 -> ignored
    agg.flush(RTH_1030 + 20)
    bars = agg.drain()
    assert len(bars) == 1
    assert bars[0]["ts"] == RTH_1030 + 10
    assert bars[0]["open"] == 10.0


def test_drain_is_idempotent():
    agg = BarAggregator()
    agg.feed(tick(RTH_1030 + 1, 10.0, 100))
    agg.flush(RTH_1030 + 10)
    assert len(agg.drain()) == 1
    assert agg.drain() == []
