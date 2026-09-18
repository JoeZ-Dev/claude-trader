import os
import sqlite3
import sys
from dataclasses import replace

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _APP_DIR)

import pytest

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
             high_water_mark=10.0, stop_level=9.5, shares=None,
             account_size_used=None, risk_pct_used=None, risk_amount_used=None):
    return OpenPosition(id=None, symbol=symbol, entry_ts=entry_ts,
                        entry_price=entry_price,
                        high_water_mark=high_water_mark, stop_level=stop_level,
                        shares=shares, account_size_used=account_size_used,
                        risk_pct_used=risk_pct_used, risk_amount_used=risk_amount_used)


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


# -- watch notes (specs.md section 7's highest-priority gap) --------------

def test_current_note_for_is_none_when_never_recorded(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    assert store.current_note_for("AEHL") is None


def test_add_watch_note_then_current_note_for_returns_it(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    store.add_watch_note("AEHL", "halted on FDA news, watching for reclaim")
    assert store.current_note_for("AEHL") == "halted on FDA news, watching for reclaim"


def test_add_watch_note_is_case_insensitive_symbol_match(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    store.add_watch_note("aehl", "lowercase add")
    assert store.current_note_for("AEHL") == "lowercase add"


def test_add_watch_note_appends_a_new_row_rather_than_overwriting(tmp_path):
    # Separate occasions of watching the same symbol can have different
    # reasons -- the OLD note must still exist in history, not be lost.
    store = JournalStore(tmp_path / "journal.db")
    store.add_watch_note("AEHL", "first reason")
    store.add_watch_note("AEHL", "second, different reason")
    assert store.current_note_for("AEHL") == "second, different reason"
    history = store.watch_note_history("AEHL")
    assert [h["note"] for h in history] == ["second, different reason", "first reason"]


def test_add_watch_note_rejects_an_over_length_note(tmp_path):
    import pytest
    from journal_store import InvalidWatchNoteError, MAX_WATCH_NOTE_LENGTH
    store = JournalStore(tmp_path / "journal.db")
    with pytest.raises(InvalidWatchNoteError):
        store.add_watch_note("AEHL", "x" * (MAX_WATCH_NOTE_LENGTH + 1))
    assert store.current_note_for("AEHL") is None  # rejected, nothing written


def test_add_watch_note_accepts_exactly_the_max_length(tmp_path):
    from journal_store import MAX_WATCH_NOTE_LENGTH
    store = JournalStore(tmp_path / "journal.db")
    note = "x" * MAX_WATCH_NOTE_LENGTH
    store.add_watch_note("AEHL", note)
    assert store.current_note_for("AEHL") == note


def test_add_watch_note_accepts_an_explicit_empty_note_as_a_real_row(tmp_path):
    # An explicit clear is a real, intentional action -- distinct from
    # "never recorded" -- and gets its own history row.
    store = JournalStore(tmp_path / "journal.db")
    store.add_watch_note("AEHL", "a real reason")
    store.add_watch_note("AEHL", "")
    assert store.current_note_for("AEHL") == ""
    assert len(store.watch_note_history("AEHL")) == 2


def test_create_persists_watch_note_snapshot(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    pos = OpenPosition(id=None, symbol="AEHL", entry_ts=100, entry_price=10.0,
                       high_water_mark=10.0, stop_level=9.5,
                       watch_note="watching for a breakout reclaim")
    created = store.create(pos)
    assert created.watch_note == "watching for a breakout reclaim"

    found = store.open_position_for("AEHL")
    assert found.watch_note == "watching for a breakout reclaim"


def test_watch_note_snapshot_survives_a_later_note_change_on_the_symbol(tmp_path):
    # The whole point: a trade's own row must answer "why was I watching
    # this" without cross-referencing watch_notes, which can change.
    store = JournalStore(tmp_path / "journal.db")
    pos = OpenPosition(id=None, symbol="AEHL", entry_ts=100, entry_price=10.0,
                       high_water_mark=10.0, stop_level=9.5,
                       watch_note="original reason at entry")
    created = store.create(pos)
    store.close_position(created, ExitEvent(exit_ts=200, exit_price=11.0,
                                            exit_reason="trailing_stop"))

    # The symbol gets a NEW note afterward (re-watched, or just updated).
    store.add_watch_note("AEHL", "a completely different later reason")
    assert store.current_note_for("AEHL") == "a completely different later reason"

    # The closed trade's own snapshot is unaffected.
    (closed,) = store.recent_closed()
    assert closed["watch_note"] == "original reason at entry"


# -- reverse-split history flag (specs.md section 7's next gap) ----------

def test_reverse_splits_for_is_empty_when_never_recorded(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    assert store.reverse_splits_for("BIAF") == []


def test_add_reverse_split_then_reverse_splits_for_returns_it(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    store.add_reverse_split("BIAF", "2024-05-02", "1:10", "pre-earnings reverse split")
    splits = store.reverse_splits_for("BIAF")
    assert len(splits) == 1
    assert splits[0]["split_date"] == "2024-05-02"
    assert splits[0]["ratio"] == "1:10"
    assert splits[0]["note"] == "pre-earnings reverse split"


def test_add_reverse_split_is_case_insensitive_symbol_match(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    store.add_reverse_split("biaf", "2024-05-02", "1:10")
    assert len(store.reverse_splits_for("BIAF")) == 1


def test_add_reverse_split_note_is_optional(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    store.add_reverse_split("BIAF", "2024-05-02", "1:10")
    assert store.reverse_splits_for("BIAF")[0]["note"] is None


def test_reverse_splits_for_accumulates_multiple_events_most_recent_first(tmp_path):
    # A symbol can have more than one reverse split over its life (common
    # among the low-float names this flag targets) -- both must survive,
    # never overwritten in place, same append-only spirit as watch_notes.
    store = JournalStore(tmp_path / "journal.db")
    store.add_reverse_split("QCLS", "2023-01-10", "1:4")
    store.add_reverse_split("QCLS", "2024-05-02", "1:10")
    splits = store.reverse_splits_for("QCLS")
    assert [s["split_date"] for s in splits] == ["2024-05-02", "2023-01-10"]


def test_reverse_splits_for_is_scoped_to_its_own_symbol(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    store.add_reverse_split("BIAF", "2024-05-02", "1:10")
    assert store.reverse_splits_for("RETO") == []


def test_add_reverse_split_rejects_a_blank_split_date(tmp_path):
    import pytest
    from journal_store import InvalidReverseSplitError
    store = JournalStore(tmp_path / "journal.db")
    with pytest.raises(InvalidReverseSplitError):
        store.add_reverse_split("BIAF", "", "1:10")
    assert store.reverse_splits_for("BIAF") == []


def test_add_reverse_split_rejects_a_non_iso_split_date(tmp_path):
    # split_date is stored as TEXT and sorted lexicographically DESC --
    # that sort is only correct for ISO 8601 (YYYY-MM-DD), so a non-ISO
    # date is rejected outright rather than silently corrupting ordering.
    import pytest
    from journal_store import InvalidReverseSplitError
    store = JournalStore(tmp_path / "journal.db")
    with pytest.raises(InvalidReverseSplitError):
        store.add_reverse_split("BIAF", "05/02/2024", "1:10")
    assert store.reverse_splits_for("BIAF") == []


def test_add_reverse_split_rejects_a_blank_ratio(tmp_path):
    import pytest
    from journal_store import InvalidReverseSplitError
    store = JournalStore(tmp_path / "journal.db")
    with pytest.raises(InvalidReverseSplitError):
        store.add_reverse_split("BIAF", "2024-05-02", "")
    assert store.reverse_splits_for("BIAF") == []


def test_add_reverse_split_rejects_an_over_length_note(tmp_path):
    import pytest
    from journal_store import InvalidReverseSplitError, MAX_REVERSE_SPLIT_NOTE_LENGTH
    store = JournalStore(tmp_path / "journal.db")
    with pytest.raises(InvalidReverseSplitError):
        store.add_reverse_split("BIAF", "2024-05-02", "1:10",
                                "x" * (MAX_REVERSE_SPLIT_NOTE_LENGTH + 1))
    assert store.reverse_splits_for("BIAF") == []


def test_watch_note_migrates_onto_an_existing_trades_table(tmp_path):
    db_path = tmp_path / "journal.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(_PARTS_A_D_SCHEMA)  # no watch_note column, no watch_notes table
    conn.execute(
        "INSERT INTO trades (symbol, entry_ts, entry_price, high_water_mark, "
        "stop_level) VALUES ('AEHL', 100, 10.0, 10.0, 9.5)",
    )
    conn.commit()
    conn.close()

    store = JournalStore(db_path)  # must not raise
    resumed = store.open_position_for("AEHL")
    assert resumed.watch_note is None
    store.add_watch_note("AEHL", "works after migration")
    assert store.current_note_for("AEHL") == "works after migration"


# -- position sizing with compounding virtual equity (specs.md section 7) --

def test_base_equity_and_risk_pct_per_trade_are_recognized_strategy_params(tmp_path):
    # Live-tunable via the EXISTING mechanism (specs.md section 8), same
    # validation/history discipline as trail_pct -- no new table needed
    # for the params themselves, only for current_equity's own tracking.
    store = JournalStore(tmp_path / "journal.db")
    store.set_param("base_equity", 5000.0)
    store.set_param("risk_pct_per_trade", 0.02)
    assert store.get_param("base_equity", 0.0) == 5000.0
    assert store.get_param("risk_pct_per_trade", 0.0) == 0.02


def test_base_equity_rejects_a_non_positive_value(tmp_path):
    from journal_store import InvalidParamError
    store = JournalStore(tmp_path / "journal.db")
    with pytest.raises(InvalidParamError):
        store.set_param("base_equity", 0.0)


def test_risk_pct_per_trade_rejects_a_value_over_its_ceiling(tmp_path):
    from journal_store import InvalidParamError
    store = JournalStore(tmp_path / "journal.db")
    with pytest.raises(InvalidParamError):
        store.set_param("risk_pct_per_trade", 0.9)


def test_current_equity_defaults_to_2000_with_no_default_params(tmp_path):
    from journal_store import DEFAULT_BASE_EQUITY
    store = JournalStore(tmp_path / "journal.db")
    assert DEFAULT_BASE_EQUITY == 2000.0
    assert store.current_equity() == 2000.0


def test_current_equity_seeds_from_default_params_base_equity_on_first_run(tmp_path):
    store = JournalStore(tmp_path / "journal.db", default_params={"base_equity": 3000.0})
    assert store.current_equity() == 3000.0


def test_current_equity_seed_is_never_reset_on_a_later_restart(tmp_path):
    # Same "already-tuned value on disk is never reset back to the
    # env/seed default" precedent as strategy_params (specs.md section 8)
    # -- main.py passes the same default_params on every startup.
    db_path = tmp_path / "journal.db"
    store1 = JournalStore(db_path, default_params={"base_equity": 3000.0})
    store1.apply_realized_pnl(trade_id=1, pnl_dollars=100.0)
    assert store1.current_equity() == 3100.0

    store2 = JournalStore(db_path, default_params={"base_equity": 3000.0})
    assert store2.current_equity() == 3100.0  # NOT reset back to 3000


def test_apply_realized_pnl_adds_to_current_equity(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    new_value = store.apply_realized_pnl(trade_id=47, pnl_dollars=42.10)
    assert new_value == pytest.approx(2042.10)
    assert store.current_equity() == pytest.approx(2042.10)


def test_apply_realized_pnl_subtracts_a_loss(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    store.apply_realized_pnl(trade_id=1, pnl_dollars=-19.565)
    assert store.current_equity() == pytest.approx(2000.0 - 19.565)


def test_apply_realized_pnl_logs_history_with_trade_close_provenance(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    store.apply_realized_pnl(trade_id=47, pnl_dollars=42.10)
    (entry,) = store.equity_history()
    assert entry["reason"] == "trade_close:trade_id=47:pnl=+42.10"
    assert entry["old_value"] == 2000.0
    assert entry["new_value"] == pytest.approx(2042.10)


def test_apply_realized_pnl_history_reason_records_a_negative_pnl_with_its_sign(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    store.apply_realized_pnl(trade_id=9, pnl_dollars=-19.565)
    (entry,) = store.equity_history()
    assert entry["reason"] == f"trade_close:trade_id=9:pnl={-19.565:+.2f}"
    assert entry["reason"].startswith("trade_close:trade_id=9:pnl=-19.5")


def test_two_same_batch_closes_apply_sequentially_neither_lost_nor_doubled(tmp_path):
    # The concurrency requirement, at the storage primitive itself: two
    # trade closes back to back, no read from anywhere else in between --
    # this is the exact same class of bug already found once in this
    # project (app.py's opened/updated/closed if/elif/elif silently
    # dropping a write when two things happened in the same batch, Part A
    # of setup-type generalization, specs.md) in a new location. Each
    # apply_realized_pnl call must read current_equity FRESH, immediately
    # before writing, never a value cached before the first call --
    # otherwise the second call would either clobber the first (lost) or
    # both would double-count against the ORIGINAL starting value.
    store = JournalStore(tmp_path / "journal.db")
    store.apply_realized_pnl(trade_id=1, pnl_dollars=-19.565)
    store.apply_realized_pnl(trade_id=2, pnl_dollars=-19.565)
    assert store.current_equity() == pytest.approx(2000.0 - 19.565 - 19.565)

    history = store.equity_history()
    assert len(history) == 2
    # Most-recent-first: trade_id=2's close applied against the value
    # trade_id=1's close had ALREADY moved, not the original 2000.0 --
    # this is what "sequential, not lost or double-applied" means here.
    assert history[0]["reason"] == f"trade_close:trade_id=2:pnl={-19.565:+.2f}"
    assert history[0]["old_value"] == pytest.approx(2000.0 - 19.565)
    assert history[0]["new_value"] == pytest.approx(2000.0 - 19.565 - 19.565)
    assert history[1]["reason"] == f"trade_close:trade_id=1:pnl={-19.565:+.2f}"
    assert history[1]["old_value"] == pytest.approx(2000.0)
    assert history[1]["new_value"] == pytest.approx(2000.0 - 19.565)


def test_reset_equity_sets_current_equity_to_the_live_base_equity_param(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    store.apply_realized_pnl(trade_id=1, pnl_dollars=250.0)
    assert store.current_equity() == pytest.approx(2250.0)

    store.set_param("base_equity", 5000.0)  # tuned AFTER the compounding above
    store.reset_equity()
    assert store.current_equity() == 5000.0  # the LIVE param, not the 2000 seed


def test_reset_equity_is_logged_distinctly_from_a_trade_driven_change(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    store.apply_realized_pnl(trade_id=1, pnl_dollars=250.0)
    store.reset_equity()
    history = store.equity_history()
    assert history[0]["reason"] == "manual_reset"
    assert history[0]["old_value"] == pytest.approx(2250.0)
    assert history[0]["new_value"] == 2000.0
    assert history[1]["reason"] == "trade_close:trade_id=1:pnl=+250.00"


def test_override_equity_sets_an_arbitrary_value_without_touching_base_equity(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    store.override_equity(777.0)
    assert store.current_equity() == 777.0
    # base_equity (what a FUTURE reset targets) is untouched by an override
    assert store.get_param("base_equity", 2000.0) == 2000.0
    store.reset_equity()
    assert store.current_equity() == 2000.0  # reset still targets the seed default


def test_override_equity_is_logged_distinctly_from_a_reset(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    store.override_equity(777.0)
    (entry,) = store.equity_history()
    assert entry["reason"] == "manual_override"
    assert entry["old_value"] == 2000.0
    assert entry["new_value"] == 777.0


def test_override_equity_rejects_a_non_positive_value(tmp_path):
    from journal_store import InvalidEquityOverrideError
    store = JournalStore(tmp_path / "journal.db")
    with pytest.raises(InvalidEquityOverrideError):
        store.override_equity(0.0)
    with pytest.raises(InvalidEquityOverrideError):
        store.override_equity(-5.0)
    assert store.current_equity() == 2000.0  # unchanged
    assert store.equity_history() == []


def test_equity_history_orders_most_recent_first_and_respects_limit(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    for i in range(3):
        store.apply_realized_pnl(trade_id=i, pnl_dollars=1.0)
    history = store.equity_history(limit=2)
    assert len(history) == 2
    assert history[0]["reason"] == "trade_close:trade_id=2:pnl=+1.00"
    assert history[1]["reason"] == "trade_close:trade_id=1:pnl=+1.00"


# -- entry-time sizing persisted on the trade row --------------------------

def test_create_persists_sizing_snapshot(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    pos = _position(shares=43, account_size_used=2000.0,
                    risk_pct_used=0.01, risk_amount_used=19.565)
    created = store.create(pos)
    assert created.shares == 43
    assert created.account_size_used == 2000.0
    assert created.risk_pct_used == 0.01
    assert created.risk_amount_used == 19.565

    found = store.open_position_for("AEHL")
    assert found.shares == 43
    assert found.account_size_used == 2000.0
    assert found.risk_pct_used == 0.01
    assert found.risk_amount_used == 19.565


def test_create_persists_a_zero_shares_sizing_snapshot_distinctly_from_unset(tmp_path):
    # 0 (a real, computed "sized down to nothing") must round-trip as 0,
    # never coerced to/confused with None ("sizing was never computed" --
    # a pre-migration position, see the migration test below).
    store = JournalStore(tmp_path / "journal.db")
    pos = _position(shares=0, account_size_used=50.0, risk_pct_used=0.01,
                    risk_amount_used=0.0)
    store.create(pos)
    found = store.open_position_for("AEHL")
    assert found.shares == 0
    assert found.shares is not None


def test_close_position_computes_realized_pnl_dollars_from_shares(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    created = store.create(_position(entry_price=9.1, shares=43))
    pnl_dollars = store.close_position(
        created, ExitEvent(exit_ts=200, exit_price=8.645, exit_reason="trailing_stop"))
    assert pnl_dollars == pytest.approx(43 * (8.645 - 9.1))
    (closed,) = store.recent_closed()
    assert closed["realized_pnl_dollars"] == pytest.approx(43 * (8.645 - 9.1))


def test_close_position_zero_shares_realized_pnl_dollars_is_zero_not_none(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    created = store.create(_position(entry_price=9.1, shares=0))
    pnl_dollars = store.close_position(
        created, ExitEvent(exit_ts=200, exit_price=8.645, exit_reason="trailing_stop"))
    assert pnl_dollars == 0.0
    assert pnl_dollars is not None
    (closed,) = store.recent_closed()
    assert closed["realized_pnl_dollars"] == 0.0


def test_close_position_with_unknown_shares_leaves_realized_pnl_dollars_null(tmp_path):
    # A pre-migration position (shares never computed) -- there is no real
    # number to record, and none must be invented.
    store = JournalStore(tmp_path / "journal.db")
    created = store.create(_position(entry_price=9.1, shares=None))
    pnl_dollars = store.close_position(
        created, ExitEvent(exit_ts=200, exit_price=8.645, exit_reason="trailing_stop"))
    assert pnl_dollars is None
    (closed,) = store.recent_closed()
    assert closed["realized_pnl_dollars"] is None


# -- migration: sizing/equity columns onto an existing DB -------------------

_PRE_SIZING_SCHEMA = """
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
    factors TEXT,
    trail_pct_used REAL,
    volume_threshold_used REAL,
    watch_note TEXT
)
"""


def test_sizing_columns_migrate_onto_an_existing_trades_table(tmp_path):
    db_path = tmp_path / "journal.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(_PRE_SIZING_SCHEMA)
    conn.execute(
        "INSERT INTO trades (symbol, entry_ts, entry_price, high_water_mark, "
        "stop_level) VALUES ('AEHL', 100, 10.0, 10.0, 9.5)",
    )
    conn.commit()
    conn.close()

    store = JournalStore(db_path)  # must not raise
    resumed = store.open_position_for("AEHL")
    assert resumed.shares is None
    assert resumed.account_size_used is None
    assert resumed.risk_pct_used is None
    assert resumed.risk_amount_used is None
    # A pre-migration open position must still close cleanly, with no
    # invented dollar P&L.
    pnl_dollars = store.close_position(
        resumed, ExitEvent(exit_ts=200, exit_price=10.5, exit_reason="trailing_stop"))
    assert pnl_dollars is None


def test_equity_state_and_history_tables_exist_on_a_db_that_predates_them(tmp_path):
    # equity_state/equity_history are CREATE TABLE IF NOT EXISTS (new
    # tables, not new columns on an existing one) -- still worth proving
    # explicitly against a DB file that predates this feature entirely,
    # same discipline as every prior schema addition this session.
    db_path = tmp_path / "journal.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(_PRE_SIZING_SCHEMA)
    conn.commit()
    conn.close()

    store = JournalStore(db_path)  # must not raise
    assert store.current_equity() == 2000.0
    store.apply_realized_pnl(trade_id=1, pnl_dollars=10.0)
    assert store.current_equity() == 2010.0


# -- two-phase exit + session-level volume gate (specs.md section 12) -----

def test_swing_low_buffer_pattern_progress_and_session_volume_multiple_are_recognized_params(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    store.set_param("swing_low_buffer_pct", 0.008)
    store.set_param("pattern_progress_threshold_pct", 0.04)
    store.set_param("session_volume_multiple", 4.0)
    assert store.get_param("swing_low_buffer_pct", 0.0) == 0.008
    assert store.get_param("pattern_progress_threshold_pct", 0.0) == 0.04
    assert store.get_param("session_volume_multiple", 0.0) == 4.0


def test_session_volume_multiple_rejects_a_value_over_its_ceiling(tmp_path):
    from journal_store import InvalidParamError
    store = JournalStore(tmp_path / "journal.db")
    with pytest.raises(InvalidParamError):
        store.set_param("session_volume_multiple", 51.0)


def test_create_persists_two_phase_exit_snapshot(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    pos = OpenPosition(id=None, symbol="AEHL", entry_ts=100, entry_price=10.0,
                       high_water_mark=10.0, stop_level=9.45,
                       exit_phase="swing_low", swing_low_buffer_pct_used=0.005,
                       pattern_progress_threshold_pct_used=0.03,
                       phase_transitioned_ts=None)
    created = store.create(pos)
    assert created.exit_phase == "swing_low"
    assert created.swing_low_buffer_pct_used == 0.005
    assert created.pattern_progress_threshold_pct_used == 0.03
    assert created.phase_transitioned_ts is None

    found = store.open_position_for("AEHL")
    assert found.exit_phase == "swing_low"
    assert found.swing_low_buffer_pct_used == 0.005
    assert found.pattern_progress_threshold_pct_used == 0.03


def test_update_trailing_persists_a_phase_transition(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    pos = OpenPosition(id=None, symbol="AEHL", entry_ts=100, entry_price=10.0,
                       high_water_mark=10.0, stop_level=9.45,
                       exit_phase="swing_low", swing_low_buffer_pct_used=0.005,
                       pattern_progress_threshold_pct_used=0.03)
    created = store.create(pos)

    transitioned = replace(created, high_water_mark=10.35, stop_level=9.83,
                          exit_phase="trailing", phase_transitioned_ts=150)
    store.update_trailing(transitioned)

    found = store.open_position_for("AEHL")
    assert found.exit_phase == "trailing"
    assert found.phase_transitioned_ts == 150
    assert found.high_water_mark == 10.35


def test_two_phase_exit_columns_migrate_onto_an_existing_trades_table(tmp_path):
    db_path = tmp_path / "journal.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(_PRE_SIZING_SCHEMA)
    conn.execute(
        "INSERT INTO trades (symbol, entry_ts, entry_price, high_water_mark, "
        "stop_level) VALUES ('AEHL', 100, 10.0, 10.0, 9.5)",
    )
    conn.commit()
    conn.close()

    store = JournalStore(db_path)  # must not raise
    resumed = store.open_position_for("AEHL")
    # A pre-migration OPEN position was already using the flat-trail-only
    # mechanism the whole time it's been open -- resuming it must NOT
    # retroactively drop it into the early swing_low phase.
    assert resumed.exit_phase == "trailing"
    assert resumed.swing_low_buffer_pct_used is None
    assert resumed.pattern_progress_threshold_pct_used is None
    assert resumed.phase_transitioned_ts is None
    # And it must still ratchet/close cleanly afterward.
    store.update_trailing(replace(resumed, high_water_mark=10.5, stop_level=9.975))
    still_open = store.open_position_for("AEHL")
    assert still_open.exit_phase == "trailing"
    assert still_open.high_water_mark == 10.5


# -- continuation-vs-fresh-day strategy params (specs.md section 7) -------

def test_continuation_lookback_days_and_threshold_pct_are_recognized_params(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    store.set_param("continuation_lookback_days", 10.0)
    store.set_param("continuation_threshold_pct", 0.75)
    assert store.get_param("continuation_lookback_days", 0.0) == 10.0
    assert store.get_param("continuation_threshold_pct", 0.0) == 0.75


def test_continuation_threshold_pct_rejects_a_value_over_its_ceiling(tmp_path):
    from journal_store import InvalidParamError
    store = JournalStore(tmp_path / "journal.db")
    with pytest.raises(InvalidParamError):
        store.set_param("continuation_threshold_pct", 6.0)
