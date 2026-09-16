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
realized_pnl_pct (nullable while open).
"""
from __future__ import annotations

import sqlite3
from dataclasses import replace

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
    realized_pnl_pct REAL
)
"""


class JournalStore:
    def __init__(self, db_path) -> None:
        # check_same_thread=False: an ASGI test client (and, in principle,
        # any WSGI/ASGI server using a worker-thread pool) may run the
        # request handling and the background poller task on different OS
        # threads even though they're on the same asyncio event loop within
        # a given thread; this connection is only ever used from whichever
        # single thread is driving that event loop at a time, never
        # concurrently, so relaxing sqlite3's same-thread check is safe
        # here rather than a real concurrency risk.
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_SCHEMA)
        self._conn.commit()

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
            "high_water_mark, stop_level) VALUES (?, ?, ?, ?, ?)",
            (position.symbol.upper(), position.entry_ts, position.entry_price,
             position.high_water_mark, position.stop_level),
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
        return [dict(r) for r in rows]


def _row_to_position(row: sqlite3.Row) -> OpenPosition:
    return OpenPosition(
        id=row["id"], symbol=row["symbol"], entry_ts=row["entry_ts"],
        entry_price=row["entry_price"], high_water_mark=row["high_water_mark"],
        stop_level=row["stop_level"],
    )
