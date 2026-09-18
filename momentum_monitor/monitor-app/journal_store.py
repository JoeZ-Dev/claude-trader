"""
journal_store.py -- SQLite persistence for the phase-4 virtual trade
journal (specs.md section 6, "Virtual trade journal" / "Storage").

Different access pattern from schwab-connector's bars (append-only,
replayed sequentially start to finish, one file per symbol) -- trade
records need to be QUERIED and reviewed (find the open one for a symbol,
list recent closed ones, eventually compute win rate/expectancy), which
SQLite fits better than another JSONL log. Don't default to JSONL just
because that's what bars used -- different access pattern, different
storage choice, on purpose.

Plain sqlite3, synchronous, no async wrapping. This is deliberate: writes
happen at most once per poll cycle (every few seconds) and a local SQLite
write takes low-single-digit milliseconds. This session already found and
fixed a real event-loop-starvation bug (reconnect.py, schwab-connector)
from a genuinely tight, high-frequency (~13/sec, sustained) blocking call
sharing an event loop -- that lesson doesn't transfer here; wrapping an
infrequent, fast, local file write in asyncio.to_thread would be solving
a problem that doesn't exist at this call frequency.

Schema (specs.md section 6): id, symbol, entry_ts, entry_price,
high_water_mark (updated live while open), stop_level (updated live
while open), exit_ts, exit_price, exit_reason (nullable while open),
realized_pnl_pct (nullable while open), setup_type (added 2026-09-17 --
which of the four setup types fired, see journal_logic.py), factors
(added 2026-09-17 -- JSON-encoded dict of the factors behind that entry
at the moment it happened: distance, trigger_price, relative_volume,
plus whatever type-specific detail setup_types.SetupCandidate.factors
carries -- so "why did this trade happen" is answerable later without
guessing from whatever's currently displayed), trail_pct_used /
volume_threshold_used (added 2026-09-18 -- the live strategy_params
values actually in effect at entry, LOCKED onto the row -- see
"strategy_params" below and journal_logic.py's OpenPosition.trail_pct).
All four nullable: a position opened before each existed has none of
that generation's columns.

Also (added 2026-09-18, specs.md section 8): `strategy_params` (key,
value, updated_at) -- the live-tunable values `Poller` reads on every
journal decision instead of a frozen env-var constant, and
`strategy_params_history` (id, key, old_value, new_value, changed_at) --
an append-only log of every change, never an in-place overwrite with no
trail. `trail_pct`/`volume_confirm_threshold` are the only two keys
today; the schema doesn't assume that stays true.

Also (added 2026-09-18, specs.md section 7's highest-priority gap):
`watch_notes` (id, symbol, note, created_at) -- a NEW row every time a
symbol is watched with a note or its note is explicitly updated, never
a single mutable field per symbol, since a symbol's reason for being
watched can genuinely differ across separate occasions and the history
of past reasons has value too (same append-only spirit as
strategy_params_history). `trades.watch_note` (added the same day) is
the SNAPSHOT of whatever was current for that symbol at the exact
moment of entry -- not a live reference to this table, which can change
after the fact.

Also (added 2026-09-18, specs.md section 7's "reverse-split history
flag" gap): `reverse_splits` (id, symbol, split_date, ratio, note,
recorded_at) -- a curated, manually-entered list (per specs.md, chosen
over an external corporate-actions API or a heuristic scan of Schwab
price history: no such data is available live from Schwab, and a new
external dependency/credential was a bigger decision than this pass
warranted). A symbol can have more than one reverse split over its
life -- the exact pattern this flag targets, low-float names that split
repeatedly -- so this is a NEW row per event, never one mutable field,
same append-only spirit as watch_notes. Deliberately NOT snapshotted
onto `trades`: unlike watch_note/trail_pct_used, a reverse split is an
immutable historical fact, not a live value that could drift out from
under an already-open position -- there is nothing to lock in at entry
that `reverse_splits_for(symbol)` doesn't already answer correctly at
any later point in time.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import replace
from pathlib import Path

from journal_logic import ExitEvent, OpenPosition

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
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
);
CREATE TABLE IF NOT EXISTS strategy_params (
    key TEXT PRIMARY KEY,
    value REAL NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_params_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    old_value REAL,
    new_value REAL NOT NULL,
    changed_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS watch_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS reverse_splits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    split_date TEXT NOT NULL,
    ratio TEXT NOT NULL,
    note TEXT,
    recorded_at INTEGER NOT NULL
)
"""


# Columns added after the table's original release -- CREATE TABLE IF NOT
# EXISTS is a no-op against an already-existing file, so an existing
# deployment's trades table needs each of these ADD COLUMN'd in explicitly
# on connect, or every read of the new column raises IndexError against a
# pre-migration row (found live 2026-09-17, deploying the setup_type/
# factors columns against the actual running journal.db -- see specs.md).
_ADDED_COLUMNS = [
    ("setup_type", "TEXT"),
    ("factors", "TEXT"),
    ("trail_pct_used", "REAL"),
    ("volume_threshold_used", "REAL"),
    ("watch_note", "TEXT"),
]

# A note longer than this is rejected outright (409), never silently
# truncated -- specs.md section 7: "reasonable length cap, rejected
# cleanly like any other validation in this app."
MAX_WATCH_NOTE_LENGTH = 500

# (lower, upper] bounds a set_param value must fall within -- "positive,
# reasonable-range" per specs.md section 8. trail_pct is a fraction (0.05 =
# 5%); 0.5 (50%) is already a far wider trailing stop than this strategy
# would ever plausibly use, chosen as a generous but non-absurd ceiling.
# volume_confirm_threshold is a multiplier of the trailing 20-bar average
# (1.5 = 50% above average); 20x average volume on a single bar is already
# an extreme outlier, not a realistic gate setting. An unknown key has no
# entry here and is rejected outright, not silently accepted with no
# bounds check -- a mistyped key should fail loudly, not write a value
# nothing ever reads.
_PARAM_BOUNDS = {
    "trail_pct": (0.0, 0.5),
    "volume_confirm_threshold": (0.0, 20.0),
}


class InvalidParamError(ValueError):
    """Raised by set_param for a value outside _PARAM_BOUNDS, or a key
    with no known bounds at all (a mistyped or unsupported key)."""


class InvalidWatchNoteError(ValueError):
    """Raised by add_watch_note for a note over MAX_WATCH_NOTE_LENGTH."""


# A note longer than this is rejected outright (409), same convention as
# MAX_WATCH_NOTE_LENGTH above -- kept as its own constant rather than
# reused so the two concerns (why-watching vs. reverse-split context)
# stay independently validated, per specs.md section 8's "each validated
# independently" precedent.
MAX_REVERSE_SPLIT_NOTE_LENGTH = 500

# split_date is stored as TEXT and reverse_splits_for sorts on it
# lexicographically DESC -- only correct for ISO 8601 (YYYY-MM-DD), so
# that's the only format add_reverse_split accepts.
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class InvalidReverseSplitError(ValueError):
    """Raised by add_reverse_split for a blank split_date/ratio, a
    non-ISO split_date, or a note over MAX_REVERSE_SPLIT_NOTE_LENGTH."""


class JournalStore:
    def __init__(self, db_path, *, default_params: dict[str, float] | None = None,
                now_fn=time.time) -> None:
        # check_same_thread=False: an ASGI test client (and, in principle,
        # any WSGI/ASGI server using a worker-thread pool) may run the
        # request handling and the background poller task on different OS
        # threads even though they're on the same asyncio event loop within
        # a given thread; this connection is only ever used from whichever
        # single thread is driving that event loop at a time, never
        # concurrently, so relaxing sqlite3's same-thread check is safe
        # here rather than a real concurrency risk.
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._now_fn = now_fn
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._migrate_added_columns()
        self._seed_params(default_params or {})
        self._conn.commit()

    def _migrate_added_columns(self) -> None:
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(trades)")}
        for name, sql_type in _ADDED_COLUMNS:
            if name not in existing:
                self._conn.execute(f"ALTER TABLE trades ADD COLUMN {name} {sql_type}")

    def _seed_params(self, defaults: dict[str, float]) -> None:
        """Seeds strategy_params from the current env-derived defaults on
        the FIRST-EVER run for each key only -- an already-tuned value
        already on disk is never overwritten back to the env default on a
        later restart (specs.md section 8), since main.py passes the same
        defaults in on every single startup, tuned or not."""
        now = int(self._now_fn())
        for key, value in defaults.items():
            existing = self._conn.execute(
                "SELECT 1 FROM strategy_params WHERE key = ?", (key,),
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    "INSERT INTO strategy_params (key, value, updated_at) VALUES (?, ?, ?)",
                    (key, value, now),
                )

    # -- live-tunable strategy parameters (specs.md section 8) ------------

    def get_param(self, key: str, default: float) -> float:
        """The live value for `key`, or `default` if it's never been set
        (no default_params were seeded for it and nobody has called
        set_param yet)."""
        row = self._conn.execute(
            "SELECT value FROM strategy_params WHERE key = ?", (key,),
        ).fetchone()
        return row["value"] if row is not None else default

    def all_params(self) -> dict[str, dict]:
        """Every current value, with when it was last changed -- the data
        behind GET /api/strategy_params."""
        rows = self._conn.execute("SELECT key, value, updated_at FROM strategy_params").fetchall()
        return {r["key"]: {"value": r["value"], "updated_at": r["updated_at"]} for r in rows}

    def set_param(self, key: str, value: float) -> None:
        """Validates against _PARAM_BOUNDS (raises InvalidParamError,
        changing NOTHING, if it fails), then updates the live value AND
        appends an old-value/new-value/timestamp row to
        strategy_params_history -- never an in-place overwrite with no
        trail (specs.md section 8's explicit requirement)."""
        bounds = _PARAM_BOUNDS.get(key)
        if bounds is None:
            raise InvalidParamError(f"{key!r} is not a recognized strategy parameter")
        lo, hi = bounds
        if not (lo < value <= hi):
            raise InvalidParamError(
                f"{key}={value!r} is out of the valid range ({lo}, {hi}]")

        now = int(self._now_fn())
        old_row = self._conn.execute(
            "SELECT value FROM strategy_params WHERE key = ?", (key,),
        ).fetchone()
        old_value = old_row["value"] if old_row is not None else None

        self._conn.execute(
            "INSERT INTO strategy_params (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, value, now),
        )
        self._conn.execute(
            "INSERT INTO strategy_params_history (key, old_value, new_value, changed_at) "
            "VALUES (?, ?, ?, ?)",
            (key, old_value, value, now),
        )
        self._conn.commit()

    def param_history(self, key: str | None = None, limit: int = 50) -> list[dict]:
        """Most-recent-first. Scoped to one `key` if given, else every
        parameter's changes interleaved by time."""
        if key is None:
            rows = self._conn.execute(
                "SELECT * FROM strategy_params_history ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM strategy_params_history WHERE key = ? "
                "ORDER BY id DESC LIMIT ?",
                (key, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- watch notes (specs.md section 7's highest-priority gap) ----------

    def add_watch_note(self, symbol: str, note: str) -> None:
        """Appends a NEW row -- never overwrites a prior note for this
        symbol in place, since separate occasions of watching the same
        symbol can genuinely have different reasons, and the history of
        past reasons has value too (same spirit as strategy_params'
        append-only history). Raises InvalidWatchNoteError (changing
        nothing) for an over-length note -- validated here, the one
        place both callers (POST /api/watch's optional note and POST
        /api/watch_note) go through, so the length cap can't be
        forgotten on one path and not the other."""
        if len(note) > MAX_WATCH_NOTE_LENGTH:
            raise InvalidWatchNoteError(
                f"note is {len(note)} characters, over the "
                f"{MAX_WATCH_NOTE_LENGTH}-character limit")
        self._conn.execute(
            "INSERT INTO watch_notes (symbol, note, created_at) VALUES (?, ?, ?)",
            (symbol.upper(), note, int(self._now_fn())),
        )
        self._conn.commit()

    def current_note_for(self, symbol: str) -> str | None:
        """The most recently recorded note for `symbol`, or None if one
        was never recorded (distinct from an explicitly-cleared note,
        which IS a real row with note="")."""
        row = self._conn.execute(
            "SELECT note FROM watch_notes WHERE symbol = ? ORDER BY id DESC LIMIT 1",
            (symbol.upper(),),
        ).fetchone()
        return row["note"] if row is not None else None

    def watch_note_history(self, symbol: str, limit: int = 50) -> list[dict]:
        """Every note ever recorded for `symbol`, most recent first --
        the "history of past reasons has value too" this table exists
        for."""
        rows = self._conn.execute(
            "SELECT * FROM watch_notes WHERE symbol = ? ORDER BY id DESC LIMIT ?",
            (symbol.upper(), limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- reverse-split history flag (specs.md section 7) -------------------

    def add_reverse_split(self, symbol: str, split_date: str, ratio: str,
                          note: str | None = None) -> None:
        """Appends a NEW row -- a symbol can have more than one reverse
        split over its life (the exact low-float pattern this flag
        targets), so a later split must never overwrite an earlier one.
        Raises InvalidReverseSplitError (changing nothing) for a blank or
        non-ISO split_date, a blank ratio, or an over-length note."""
        if not split_date or not _ISO_DATE.match(split_date):
            raise InvalidReverseSplitError(
                f"split_date {split_date!r} must be an ISO date (YYYY-MM-DD)")
        if not ratio or not ratio.strip():
            raise InvalidReverseSplitError("ratio is required")
        if note is not None and len(note) > MAX_REVERSE_SPLIT_NOTE_LENGTH:
            raise InvalidReverseSplitError(
                f"note is {len(note)} characters, over the "
                f"{MAX_REVERSE_SPLIT_NOTE_LENGTH}-character limit")
        self._conn.execute(
            "INSERT INTO reverse_splits (symbol, split_date, ratio, note, recorded_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (symbol.strip().upper(), split_date, ratio.strip(), note, int(self._now_fn())),
        )
        self._conn.commit()

    def reverse_splits_for(self, symbol: str) -> list[dict]:
        """Every recorded reverse split for `symbol`, most recent
        split_date first -- [] if none were ever recorded."""
        rows = self._conn.execute(
            "SELECT * FROM reverse_splits WHERE symbol = ? "
            "ORDER BY split_date DESC, id DESC",
            (symbol.strip().upper(),),
        ).fetchall()
        return [dict(r) for r in rows]

    def open_position_for(self, symbol: str) -> OpenPosition | None:
        """The currently-open (exit_ts IS NULL) position for `symbol`, if
        any. Used both when starting to watch a symbol and to resume
        across a restart (specs.md: an open position must survive a
        container restart, proven by test, not assumed)."""
        row = self._conn.execute(
            "SELECT * FROM trades WHERE symbol = ? AND exit_ts IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (symbol.upper(),),
        ).fetchone()
        return _row_to_position(row) if row else None

    def create(self, position: OpenPosition) -> OpenPosition:
        cur = self._conn.execute(
            "INSERT INTO trades (symbol, entry_ts, entry_price, "
            "high_water_mark, stop_level, setup_type, factors, "
            "trail_pct_used, volume_threshold_used, watch_note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (position.symbol.upper(), position.entry_ts, position.entry_price,
             position.high_water_mark, position.stop_level, position.setup_type,
             json.dumps(position.factors) if position.factors is not None else None,
             position.trail_pct, position.volume_threshold_used, position.watch_note),
        )
        self._conn.commit()
        return replace(position, id=cur.lastrowid)

    def update_trailing(self, position: OpenPosition) -> None:
        self._conn.execute(
            "UPDATE trades SET high_water_mark = ?, stop_level = ? WHERE id = ?",
            (position.high_water_mark, position.stop_level, position.id),
        )
        self._conn.commit()

    def close_position(self, position: OpenPosition, exit_event: ExitEvent) -> None:
        pnl_pct = ((exit_event.exit_price - position.entry_price)
                  / position.entry_price * 100.0)
        self._conn.execute(
            "UPDATE trades SET exit_ts = ?, exit_price = ?, exit_reason = ?, "
            "realized_pnl_pct = ? WHERE id = ?",
            (exit_event.exit_ts, exit_event.exit_price, exit_event.exit_reason,
             pnl_pct, position.id),
        )
        self._conn.commit()

    def recent_closed(self, limit: int = 10) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE exit_ts IS NOT NULL "
            "ORDER BY exit_ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_decode_factors(dict(r)) for r in rows]

    def delete_closed(self, trade_id: int) -> bool:
        """Permanently deletes one CLOSED trade row by id. Returns True if
        a row was actually removed, False if no closed row with that id
        existed (a no-op, not an error -- same convention as
        Poller.remove_symbol's False-on-unknown-symbol). Restricted to
        closed rows (exit_ts IS NOT NULL) on purpose: deleting an OPEN
        position's row here would silently desync it from Poller's own
        in-memory _SymbolSlot.journal_position, which has no way to learn
        the row vanished underneath it -- this only ever removes history,
        never an active position."""
        cur = self._conn.execute(
            "DELETE FROM trades WHERE id = ? AND exit_ts IS NOT NULL", (trade_id,),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def delete_symbol_switched(self) -> int:
        """Permanently deletes every CLOSED trade row whose exit_reason is
        'symbol_switched' -- the bulk "clear housekeeping noise" action
        (specs.md section 6). Returns the number of rows removed."""
        cur = self._conn.execute(
            "DELETE FROM trades WHERE exit_reason = 'symbol_switched' "
            "AND exit_ts IS NOT NULL",
        )
        self._conn.commit()
        return cur.rowcount


# The dataclass's own default, not a re-typed literal -- a pre-migration
# row (trail_pct_used NULL) resumes using whatever OpenPosition itself
# considers "no value given," so the two can never silently drift apart.
_DEFAULT_TRAIL_PCT = OpenPosition.__dataclass_fields__["trail_pct"].default


def _row_to_position(row: sqlite3.Row) -> OpenPosition:
    return OpenPosition(
        id=row["id"], symbol=row["symbol"], entry_ts=row["entry_ts"],
        entry_price=row["entry_price"], high_water_mark=row["high_water_mark"],
        stop_level=row["stop_level"], setup_type=row["setup_type"],
        factors=json.loads(row["factors"]) if row["factors"] is not None else None,
        trail_pct=(row["trail_pct_used"] if row["trail_pct_used"] is not None
                   else _DEFAULT_TRAIL_PCT),
        volume_threshold_used=row["volume_threshold_used"],
        watch_note=row["watch_note"],
    )


def _decode_factors(row: dict) -> dict:
    if row.get("factors") is not None:
        row["factors"] = json.loads(row["factors"])
    return row
