import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store import BarStore


def bar(ts, close, *, vol=1000.0, ext=False):
    return {
        "ts": ts, "open": close, "high": close, "low": close,
        "close": close, "volume": vol, "is_extended": ext,
    }


def test_append_then_since_zero_returns_all(tmp_path):
    s = BarStore(tmp_path)
    s.append("AAPL", bar(100, 10.0))
    s.append("AAPL", bar(110, 10.5))
    s.append("AAPL", bar(120, 10.2))
    assert [b["ts"] for b in s.since("AAPL", 0)] == [100, 110, 120]


def test_since_ts_is_inclusive_lower_bound(tmp_path):
    s = BarStore(tmp_path)
    for ts in (100, 110, 120):
        s.append("AAPL", bar(ts, 1.0))
    assert [b["ts"] for b in s.since("AAPL", 110)] == [110, 120]


def test_persists_across_new_instance(tmp_path):
    BarStore(tmp_path).append("MSFT", bar(200, 42.0))
    reopened = BarStore(tmp_path)
    got = reopened.since("MSFT", 0)
    assert len(got) == 1 and got[0]["ts"] == 200


def test_unknown_symbol_returns_empty(tmp_path):
    assert BarStore(tmp_path).since("NOPE", 0) == []


def test_symbol_lookup_is_case_insensitive(tmp_path):
    s = BarStore(tmp_path)
    s.append("aapl", bar(100, 10.0))
    assert len(s.since("AAPL", 0)) == 1


def test_symbols_lists_written_symbols(tmp_path):
    s = BarStore(tmp_path)
    s.append("AAPL", bar(1, 1.0))
    s.append("TSLA", bar(1, 1.0))
    assert s.symbols() == ["AAPL", "TSLA"]


def test_bar_round_trips_exact_shape(tmp_path):
    s = BarStore(tmp_path)
    original = bar(100, 9.87, vol=54321.0, ext=True)
    s.append("AEHL", original)
    (got,) = BarStore(tmp_path).since("AEHL", 0)
    assert got == original
    assert got["is_extended"] is True


def test_append_many(tmp_path):
    s = BarStore(tmp_path)
    s.append_many("AAPL", [bar(100, 1.0), bar(110, 2.0), bar(120, 3.0)])
    assert [b["ts"] for b in s.since("AAPL", 0)] == [100, 110, 120]


def test_append_ignores_non_monotonic_ts(tmp_path):
    # A replay restart (or a stream reconnect that re-sends a bar) must not
    # duplicate or rewind already-stored bars.
    s = BarStore(tmp_path)
    s.append_many("AAPL", [bar(100, 1.0), bar(110, 2.0)])
    s.append("AAPL", bar(110, 9.9))   # duplicate ts -> ignored
    s.append("AAPL", bar(100, 9.9))   # older ts     -> ignored
    s.append("AAPL", bar(120, 3.0))   # newer ts     -> kept
    got = s.since("AAPL", 0)
    assert [(b["ts"], b["close"]) for b in got] == [(100, 1.0), (110, 2.0), (120, 3.0)]


def test_non_monotonic_guard_persists_across_reopen(tmp_path):
    BarStore(tmp_path).append_many("AAPL", [bar(100, 1.0), bar(110, 2.0)])
    s2 = BarStore(tmp_path)              # reopen: last-ts known from disk
    s2.append("AAPL", bar(105, 5.0))    # older than stored max -> ignored
    s2.append("AAPL", bar(115, 5.0))    # newer -> kept
    assert [b["ts"] for b in s2.since("AAPL", 0)] == [100, 110, 115]
