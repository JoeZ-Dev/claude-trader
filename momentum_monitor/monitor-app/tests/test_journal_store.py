import os
import sys

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _APP_DIR)

from journal_logic import ExitEvent, OpenPosition
from journal_store import JournalStore


def _position(symbol="AEHL", entry_ts=100, entry_price=10.0,
             high_water_mark=10.0, stop_level=9.5):
    return OpenPosition(id=None, symbol=symbol, entry_ts=entry_ts,
                        entry_price=entry_price,
                        high_water_mark=high_water_mark, stop_level=stop_level)


def test_open_position_for_returns_none_when_nothing_open(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    assert store.open_position_for("AEHL") is None


def test_creates_parent_directory_if_missing(tmp_path):
    # Matches BarStore's (schwab-connector/store.py) pattern -- the docker
    # volume mount point exists by the time the container runs, but a
    # fresh checkout / local run shouldn't need to pre-create it by hand.
    db_path = tmp_path / "nested" / "dir" / "journal.db"
    store = JournalStore(db_path)
    store.create(_position())
    assert db_path.exists()


def test_create_then_open_position_for_returns_it(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    created = store.create(_position())
    assert created.id is not None

    found = store.open_position_for("AEHL")
    assert found is not None
    assert found.id == created.id
    assert found.entry_price == 10.0
    assert found.high_water_mark == 10.0
    assert found.stop_level == 9.5


def test_create_persists_setup_type_and_factors(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    pos = OpenPosition(id=None, symbol="AEHL", entry_ts=100, entry_price=10.0,
                       high_water_mark=10.0, stop_level=9.5,
                       setup_type="micro_breakout",
                       factors={"strength_score": 5.0, "distance": 0.3,
                               "relative_volume": 1.8})
    created = store.create(pos)
    assert created.setup_type == "micro_breakout"
    assert created.factors == {"strength_score": 5.0, "distance": 0.3,
                               "relative_volume": 1.8}

    found = store.open_position_for("AEHL")
    assert found.setup_type == "micro_breakout"
    assert found.factors == {"strength_score": 5.0, "distance": 0.3,
                             "relative_volume": 1.8}


def test_setup_type_and_factors_are_retrievable_on_a_closed_trade(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    pos = OpenPosition(id=None, symbol="AEHL", entry_ts=100, entry_price=10.0,
                       high_water_mark=10.0, stop_level=9.5,
                       setup_type="vwap_reclaim",
                       factors={"vwap": 10.05, "relative_volume": 2.1})
    created = store.create(pos)
    store.close_position(created, ExitEvent(exit_ts=200, exit_price=11.0,
                                            exit_reason="trailing_stop"))
    (closed,) = store.recent_closed()
    assert closed["setup_type"] == "vwap_reclaim"
    assert closed["factors"] == {"vwap": 10.05, "relative_volume": 2.1}


def test_open_position_for_is_case_insensitive_symbol_match(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    store.create(_position(symbol="AEHL"))
    assert store.open_position_for("aehl") is not None


def test_update_trailing_persists_new_high_water_mark_and_stop(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    created = store.create(_position())
    updated = created.__class__(id=created.id, symbol=created.symbol,
                                entry_ts=created.entry_ts,
                                entry_price=created.entry_price,
                                high_water_mark=11.0, stop_level=10.45)
    store.update_trailing(updated)

    found = store.open_position_for("AEHL")
    assert found.high_water_mark == 11.0
    assert found.stop_level == 10.45


def test_close_position_removes_it_from_open_position_for(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    created = store.create(_position())
    store.close_position(created, ExitEvent(exit_ts=200, exit_price=10.9,
                                            exit_reason="trailing_stop"))
    assert store.open_position_for("AEHL") is None


def test_close_position_computes_realized_pnl_pct(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    created = store.create(_position(entry_price=10.0))
    store.close_position(created, ExitEvent(exit_ts=200, exit_price=11.0,
                                            exit_reason="trailing_stop"))
    (closed,) = store.recent_closed()
    assert closed["realized_pnl_pct"] == 10.0  # (11-10)/10 * 100


def test_close_position_records_exit_reason_and_price(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    created = store.create(_position())
    store.close_position(created, ExitEvent(exit_ts=200, exit_price=9.3,
                                            exit_reason="symbol_switched"))
    (closed,) = store.recent_closed()
    assert closed["exit_reason"] == "symbol_switched"
    assert closed["exit_price"] == 9.3
    assert closed["exit_ts"] == 200


def test_recent_closed_excludes_still_open_positions(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    open_pos = store.create(_position(symbol="OPEN"))
    closed_pos = store.create(_position(symbol="CLOSED"))
    store.close_position(closed_pos, ExitEvent(exit_ts=200, exit_price=10.5,
                                               exit_reason="trailing_stop"))
    closed = store.recent_closed()
    assert [c["symbol"] for c in closed] == ["CLOSED"]


def test_recent_closed_orders_most_recent_first_and_respects_limit(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    for i in range(3):
        pos = store.create(_position(symbol=f"S{i}", entry_ts=i))
        store.close_position(pos, ExitEvent(exit_ts=100 + i, exit_price=10.0,
                                            exit_reason="trailing_stop"))
    closed = store.recent_closed(limit=2)
    assert len(closed) == 2
    assert [c["symbol"] for c in closed] == ["S2", "S1"]


# -- restart persistence: prove it, don't assume it --------------------

def test_open_position_survives_reopening_the_store_over_the_same_file(tmp_path):
    db_path = tmp_path / "journal.db"
    store1 = JournalStore(db_path)
    created = store1.create(_position(symbol="AEHL", entry_price=10.0,
                                      high_water_mark=10.5, stop_level=9.975))
    del store1  # simulates the process ending

    # Fresh JournalStore instance over the same file = a restart.
    store2 = JournalStore(db_path)
    resumed = store2.open_position_for("AEHL")
    assert resumed is not None
    assert resumed.id == created.id
    assert resumed.entry_price == 10.0
    assert resumed.high_water_mark == 10.5
    assert resumed.stop_level == 9.975


def test_closed_trades_survive_reopening_the_store_over_the_same_file(tmp_path):
    db_path = tmp_path / "journal.db"
    store1 = JournalStore(db_path)
    pos = store1.create(_position(symbol="AEHL"))
    store1.close_position(pos, ExitEvent(exit_ts=200, exit_price=11.0,
                                         exit_reason="trailing_stop"))
    del store1

    store2 = JournalStore(db_path)
    closed = store2.recent_closed()
    assert len(closed) == 1
    assert closed[0]["exit_reason"] == "trailing_stop"


# -- deleting closed trades ----------------------------------------------

def test_delete_closed_removes_the_row_and_returns_true(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    pos = store.create(_position(symbol="AEHL"))
    store.close_position(pos, ExitEvent(exit_ts=200, exit_price=11.0,
                                        exit_reason="trailing_stop"))
    (closed,) = store.recent_closed()

    assert store.delete_closed(closed["id"]) is True
    assert store.recent_closed() == []


def test_delete_closed_unknown_id_is_a_noop_returns_false(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    assert store.delete_closed(999) is False


def test_delete_closed_never_removes_an_open_position(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    open_pos = store.create(_position(symbol="AEHL"))
    assert store.delete_closed(open_pos.id) is False
    assert store.open_position_for("AEHL") is not None


def test_delete_symbol_switched_removes_only_those_rows_and_returns_count(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    p1 = store.create(_position(symbol="AEHL"))
    store.close_position(p1, ExitEvent(exit_ts=100, exit_price=11.0,
                                       exit_reason="symbol_switched"))
    p2 = store.create(_position(symbol="MSFT"))
    store.close_position(p2, ExitEvent(exit_ts=200, exit_price=9.0,
                                       exit_reason="trailing_stop"))
    p3 = store.create(_position(symbol="NVDA"))
    store.close_position(p3, ExitEvent(exit_ts=300, exit_price=12.0,
                                       exit_reason="symbol_switched"))

    deleted_count = store.delete_symbol_switched()

    assert deleted_count == 2
    remaining = store.recent_closed()
    assert [c["symbol"] for c in remaining] == ["MSFT"]


def test_delete_symbol_switched_returns_zero_when_none_exist(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    pos = store.create(_position(symbol="AEHL"))
    store.close_position(pos, ExitEvent(exit_ts=100, exit_price=11.0,
                                        exit_reason="trailing_stop"))
    assert store.delete_symbol_switched() == 0
    assert len(store.recent_closed()) == 1
