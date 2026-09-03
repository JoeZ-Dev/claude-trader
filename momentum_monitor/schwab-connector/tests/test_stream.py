import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aggregator import BarAggregator
from stream import ReplayStreamSource, message_to_ticks


def drain(agen, limit=None):
    async def _run():
        out = []
        async for item in agen:
            out.append(item)
            if limit is not None and len(out) >= limit:
                break
        return out

    return asyncio.run(_run())


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_replay_from_tick_fixture_yields_in_order(tmp_path):
    fx = tmp_path / "ticks.jsonl"
    rows = [
        {"ts": 1000.0, "price": 10.0, "size": 100},
        {"ts": 1001.0, "price": 10.2, "size": 50},
        {"ts": 1002.5, "price": 9.9, "size": 25},
    ]
    write_jsonl(fx, rows)
    got = drain(ReplayStreamSource(fx).ticks("AEHL"))
    assert [(t["ts"], t["price"], t["size"]) for t in got] == [
        (1000.0, 10.0, 100), (1001.0, 10.2, 50), (1002.5, 9.9, 25),
    ]


def test_replay_from_bar_fixture_reconstructs_bars_through_aggregator(tmp_path):
    fx = tmp_path / "bars.jsonl"
    bars = [
        {"ts": 1000, "open": 10.0, "high": 10.8, "low": 9.7, "close": 10.3,
         "volume": 5000.0, "is_extended": False},
        {"ts": 1010, "open": 10.3, "high": 10.4, "low": 10.0, "close": 10.1,
         "volume": 3000.0, "is_extended": False},
    ]
    write_jsonl(fx, bars)

    agg = BarAggregator()
    for tick in drain(ReplayStreamSource(fx).ticks("AEHL")):
        agg.feed(tick)
    agg.flush(1020)  # just past the last real bucket: finalize, no gap-fill
    out = agg.drain()

    assert [(b["ts"], b["open"], b["high"], b["low"], b["close"], b["volume"])
            for b in out] == [
        (1000, 10.0, 10.8, 9.7, 10.3, 5000.0),
        (1010, 10.3, 10.4, 10.0, 10.1, 3000.0),
    ]


def test_replay_bar_explosion_stays_within_one_bucket(tmp_path):
    fx = tmp_path / "one.jsonl"
    write_jsonl(fx, [{"ts": 2000, "open": 5.0, "high": 6.0, "low": 4.0,
                      "close": 5.5, "volume": 999.0, "is_extended": True}])
    ticks = drain(ReplayStreamSource(fx).ticks("X"))
    assert all(2000 <= t["ts"] < 2010 for t in ticks)
    assert sum(t["size"] for t in ticks) == 999.0


SAMPLE_NAMED = {
    "service": "LEVELONE_EQUITIES",
    "timestamp": 1700000000000,
    "content": [{"key": "AEHL", "LAST_PRICE": 8.69, "LAST_SIZE": 300,
                 "TOTAL_VOLUME": 1_200_000}],
}
SAMPLE_NUMERIC = {
    "service": "LEVELONE_EQUITIES",
    "timestamp": 1700000000000,
    "content": [{"key": "AEHL", "3": 8.69, "9": 300, "8": 1_200_000}],
}


def test_message_to_ticks_named_fields():
    ticks = message_to_ticks(SAMPLE_NAMED)
    assert ticks == [("AEHL", {"ts": 1700000000.0, "price": 8.69, "size": 300.0})]


def test_message_to_ticks_numeric_fields():
    ticks = message_to_ticks(SAMPLE_NUMERIC)
    assert ticks == [("AEHL", {"ts": 1700000000.0, "price": 8.69, "size": 300.0})]


def test_message_to_ticks_price_without_size_is_zero_volume():
    msg = {"timestamp": 1700000000000,
           "content": [{"key": "AEHL", "LAST_PRICE": 8.7}]}
    ticks = message_to_ticks(msg)
    assert ticks == [("AEHL", {"ts": 1700000000.0, "price": 8.7, "size": 0.0})]


def test_message_to_ticks_skips_entries_without_price():
    msg = {"timestamp": 1700000000000,
           "content": [{"key": "AEHL", "BID_PRICE": 8.6},
                       {"key": "MSFT", "LAST_PRICE": 420.0}]}
    ticks = message_to_ticks(msg)
    assert ticks == [("MSFT", {"ts": 1700000000.0, "price": 420.0, "size": 0.0})]


def test_replay_source_reports_connected_after_start(tmp_path):
    fx = tmp_path / "t.jsonl"
    write_jsonl(fx, [{"ts": 1.0, "price": 1.0, "size": 1}])
    src = ReplayStreamSource(fx)
    assert src.connected is False
    drain(src.ticks("X"))
    assert src.connected is True
