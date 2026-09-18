import os
import sqlite3
import sys

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _APP_DIR)

from journal_logic import ExitEvent, OpenPosition
from journal_store import JournalStore


_PRE_MIGRATION_SCHEMA = """
CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    entry_ts INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    high_water_mark REAL NOT NULL,
    stop_level REAL NOT NULL,
    exit_ts INTEGER,
    exit_price REAL,
    exit_reason TEXT,
    realized_pnl_pct REAL
)
"""


def test_opens_a_pre_migration_db_missing_setup_type_and_factors_columns(tmp_path):
    # Found live 2026-09-17: CREATE TABLE IF NOT EXISTS is a no-op against
    # an already-existing file, so deploying the setup_type/factors
    # columns against a running journal.db crashed every open_position_for
    # call with IndexError the instant it read a pre-existing row. A
    # JournalStore opening an old-schema file must migrate it in place,
    # not just work against a fresh one.
    db_path = tmp_path / "journal.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(_PRE_MIGRATION_SCHEMA)
    conn.execute(
        "INSERT INTO trades (symbol, entry_ts, entry_price, high_water_mark, "
        "stop_level) VALUES ('AEHL', 100, 10.0, 10.0, 9.5)",
    )
    conn.commit()
    conn.close()

    store = JournalStore(db_path)  # must not raise
    resumed = store.open_position_for("AEHL")
    assert resumed is not None
    assert resumed.entry_price == 10.0
    assert resumed.setup_type is None    # pre-migration row has neither
    assert resumed.factors is None

    # And the migrated table actually accepts new rows using the new
    # columns going forward, not just tolerating old ones.
    created = store.create(OpenPosition(
        id=None, symbol="MSFT", entry_ts=200, entry_price=20.0,
        high_water_mark=20.0, stop_level=19.0,
        setup_type="vwap_reclaim", factors={"vwap": 19.9},
    ))
    assert store.open_position_for("MSFT").setup_type == "vwap_reclaim"
    assert created.factors == {"vwap": 19.9}


_PARTS_A_D_SCHEMA = """
CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    entry_ts INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    high_water_mark REAL NOT NULL,
    stop_level REAL NOT NULL,
    exit_ts INTEGER,
    exit_price REAL,
    exit_reason TEXT,
    realized_pnl_pct REAL,
    setup_type TEXT,
    factors TEXT
)
"""


def test_migrates_a_parts_a_d_db_missing_trail_pct_and_volume_threshold_used(tmp_path):
    # Some trades may have fired under the Parts A-D schema (setup_type/
    # factors, but no trail_pct_used/volume_threshold_used yet) before this
    # build landed -- migrate those rows explicitly rather than assuming a
    # fresh table (this project has hit exactly the "assumed fresh, wasn't"
    # bug multiple times already, see specs.md).
    db_path = tmp_path / "journal.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(_PARTS_A_D_SCHEMA)
    conn.execute(
        "INSERT INTO trades (symbol, entry_ts, entry_price, high_water_mark, "
        "stop_level, setup_type, factors) VALUES "
        "('AEHL', 100, 10.0, 10.0, 9.5, 'round_number_reclaim', '{}')",
    )
    conn.commit()
    conn.close()

    store = JournalStore(db_path)  # must not raise
    resumed = store.open_position_for("AEHL")
    assert resumed is not None
    assert resumed.setup_type == "round_number_reclaim"
    assert resumed.trail_pct == 0.05          # pre-migration row: dataclass default
    assert resumed.volume_threshold_used is None


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


# -- trail_pct_used / volume_threshold_used persisted on the trade row ----

def test_create_persists_trail_pct_used_and_volume_threshold_used(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    pos = OpenPosition(id=None, symbol="AEHL", entry_ts=100, entry_price=10.0,
                       high_water_mark=10.0, stop_level=9.3,
                       trail_pct=0.07, volume_threshold_used=2.0)
    created = store.create(pos)
    assert created.trail_pct == 0.07
    assert created.volume_threshold_used == 2.0

    found = store.open_position_for("AEHL")
    assert found.trail_pct == 0.07
    assert found.volume_threshold_used == 2.0


def test_trail_pct_used_retrievable_on_a_closed_trade(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    pos = OpenPosition(id=None, symbol="AEHL", entry_ts=100, entry_price=10.0,
                       high_water_mark=10.0, stop_level=9.3,
                       trail_pct=0.07, volume_threshold_used=2.0)
    created = store.create(pos)
    store.close_position(created, ExitEvent(exit_ts=200, exit_price=9.3,
                                            exit_reason="trailing_stop"))
    (closed,) = store.recent_closed()
    assert closed["trail_pct_used"] == 0.07
    assert closed["volume_threshold_used"] == 2.0


# -- live-tunable strategy_params (specs.md section 8) ---------------------

def test_seeds_params_from_defaults_on_first_run(tmp_path):
    store = JournalStore(tmp_path / "journal.db",
                         default_params={"trail_pct": 0.05,
                                         "volume_confirm_threshold": 1.5})
    assert store.get_param("trail_pct", default=0.99) == 0.05
    assert store.get_param("volume_confirm_threshold", default=0.99) == 1.5


def test_reopening_the_store_does_not_re_seed_over_a_changed_value(tmp_path):
    db_path = tmp_path / "journal.db"
    store1 = JournalStore(db_path, default_params={"trail_pct": 0.05})
    store1.set_param("trail_pct", 0.10)
    del store1

    # A fresh JournalStore over the SAME file, passing the SAME defaults
    # again (exactly what main.py does on every restart) -- must not
    # silently reset the tuned value back to the env-var default.
    store2 = JournalStore(db_path, default_params={"trail_pct": 0.05})
    assert store2.get_param("trail_pct", default=0.99) == 0.10


def test_get_param_returns_the_given_default_when_key_never_set(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    assert store.get_param("trail_pct", default=0.05) == 0.05


def test_set_param_updates_the_live_value(tmp_path):
    store = JournalStore(tmp_path / "journal.db",
                         default_params={"trail_pct": 0.05})
    store.set_param("trail_pct", 0.08)
    assert store.get_param("trail_pct", default=0.99) == 0.08


def test_set_param_records_old_new_and_timestamp_in_history(tmp_path):
    store = JournalStore(tmp_path / "journal.db",
                         default_params={"trail_pct": 0.05},
                         now_fn=lambda: 1_800_000_000)
    store.set_param("trail_pct", 0.08)
    history = store.param_history()
    assert len(history) == 1
    entry = history[0]
    assert entry["key"] == "trail_pct"
    assert entry["old_value"] == 0.05
    assert entry["new_value"] == 0.08
    assert entry["changed_at"] == 1_800_000_000


def test_set_param_history_accumulates_across_multiple_changes(tmp_path):
    store = JournalStore(tmp_path / "journal.db",
                         default_params={"trail_pct": 0.05})
    store.set_param("trail_pct", 0.08)
    store.set_param("trail_pct", 0.10)
    history = store.param_history()
    assert [h["new_value"] for h in history] == [0.10, 0.08]  # most recent first
    assert history[0]["old_value"] == 0.08


def test_set_param_first_ever_change_has_null_old_value(tmp_path):
    store = JournalStore(tmp_path / "journal.db")  # no default_params seeded
    store.set_param("trail_pct", 0.08)
    history = store.param_history()
    assert history[0]["old_value"] is None
    assert history[0]["new_value"] == 0.08


def test_all_params_lists_current_values_with_updated_at(tmp_path):
    store = JournalStore(tmp_path / "journal.db",
                         default_params={"trail_pct": 0.05,
                                         "volume_confirm_threshold": 1.5},
                         now_fn=lambda: 1_800_000_000)
    params = store.all_params()
    assert params["trail_pct"]["value"] == 0.05
    assert params["trail_pct"]["updated_at"] == 1_800_000_000
    assert params["volume_confirm_threshold"]["value"] == 1.5


def test_set_param_rejects_zero():
    import pytest
    from journal_store import InvalidParamError
    store = JournalStore(":memory:")
    with pytest.raises(InvalidParamError):
        store.set_param("trail_pct", 0.0)


def test_set_param_rejects_negative():
    import pytest
    from journal_store import InvalidParamError
    store = JournalStore(":memory:")
    with pytest.raises(InvalidParamError):
        store.set_param("trail_pct", -0.05)


def test_set_param_rejects_absurdly_large_trail_pct():
    import pytest
    from journal_store import InvalidParamError
    store = JournalStore(":memory:")
    with pytest.raises(InvalidParamError):
        store.set_param("trail_pct", 5.0)  # 500% is not a trailing stop


def test_set_param_rejects_absurdly_large_volume_threshold():
    import pytest
    from journal_store import InvalidParamError
    store = JournalStore(":memory:")
    with pytest.raises(InvalidParamError):
        store.set_param("volume_confirm_threshold", 500.0)


def test_set_param_rejects_unknown_key():
    import pytest
    from journal_store import InvalidParamError
    store = JournalStore(":memory:")
    with pytest.raises(InvalidParamError):
        store.set_param("not_a_real_param", 1.0)


def test_a_rejected_set_param_does_not_change_the_live_value_or_history(tmp_path):
    import pytest
    from journal_store import InvalidParamError
    store = JournalStore(tmp_path / "journal.db",
                         default_params={"trail_pct": 0.05})
    with pytest.raises(InvalidParamError):
        store.set_param("trail_pct", -1.0)
    assert store.get_param("trail_pct", default=0.99) == 0.05
    assert store.param_history() == []
