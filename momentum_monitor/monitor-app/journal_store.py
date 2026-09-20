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

Also (added 2026-09-18, specs.md section 7's position-sizing gap):
`trades` gains `shares`/`account_size_used`/`risk_pct_used`/
`risk_amount_used` (entry-time sizing, LOCKED onto the row the same way
as trail_pct_used -- see journal_logic.py's OpenPosition) and
`realized_pnl_dollars` (computed at close from the position's own
`shares`, alongside the existing `realized_pnl_pct`). All nullable: a
position opened before these existed has none of them, and a genuinely
computed zero-share entry (shares=0, a real outcome, see specs.md) is
never confused with "sizing was never computed" (shares=NULL) -- the
two read identically in casual display but are stored, and must stay
distinguishable, as different things. `base_equity`/`risk_pct_per_trade`
reuse the EXISTING `strategy_params`/`strategy_params_history` mechanism
above (live-tunable, same validation/history discipline as trail_pct) --
no new table needed for the params themselves. `current_equity` -- the
actual running virtual account balance a new entry sizes against -- is
different in kind from those and gets its own two tables:
`equity_state` (a single row, `id` fixed at 1, holding the current live
value) and `equity_history` (id, old_value, new_value, `reason`,
changed_at) -- an append-only log like strategy_params_history's, but
with a `reason` field in place of a bare key, since current_equity
changes through three DIFFERENT kinds of action (a real trade closing,
an explicit reset to base_equity, an explicit manual override) that
must stay distinguishable later, not just old-value/new-value pairs
with no indication of WHICH path produced them.

Also (added 2026-09-18, specs.md section 12): `trades` gains
`exit_phase`/`swing_low_buffer_pct_used`/
`pattern_progress_threshold_pct_used`/`phase_transitioned_ts` for the
two-phase exit (see journal_logic.py's OpenPosition). Unlike every
prior addition's migration story, a NULL `exit_phase` on an existing
row does NOT mean "defaulted the same as a brand-new position" --
`_row_to_position` explicitly reads a NULL back as `"trailing"`, never
`"trailing_stop"` phase's dataclass-default sibling `"swing_low"`,
because a pre-migration OPEN position was already using the original
flat-trail-only mechanism the whole time it's been open; resuming it
into the early phase would be a real, wrong, retroactive behavior
change for a trade already in flight, not a neutral default.
`swing_low_buffer_pct`/`pattern_progress_threshold_pct`/
`session_volume_multiple` (the session-level volume gate, same
section) join `base_equity`/`risk_pct_per_trade` in the EXISTING
`strategy_params` mechanism -- no new table.

Also (added 2026-09-18, specs.md section 7's continuation-vs-fresh-day
gap): `continuation_lookback_days`/`continuation_threshold_pct` join
`strategy_params` the same way -- an INFORMATIONAL flag, never
snapshotted onto `trades` at all (unlike every entry-time-locked value
above), since it never gates or influences an entry decision and is
meant to reflect whatever the CURRENT live threshold says whenever a
symbol's panel is viewed, not a value frozen at some past moment.

Also (added 2026-09-19, specs.md section 24, "human review/labeling" --
section 7's last remaining data-collection gap): `trades` gains
`review_label`/`review_note`/`ideal_entry_price` -- a human's OWN
after-the-fact judgment about an already-CLOSED trade, the first place
in this whole journal a human opinion gets attached to a mechanically-
logged decision rather than something the system computed about itself.
Unlike watch_notes/reverse_splits/strategy_params_history, these are
PLAIN mutable columns with no separate append-only history table:
reviewing a trade again just overwrites the existing values in place --
a symbol's reason for being watched can genuinely differ across
separate occasions (which is why watch_notes needed history), but a
review is a single, current, replaceable judgment about one already-
finished, immutable-outcome trade, not a sequence of distinct past
events worth preserving individually. `review_label` is constrained to
`REVIEW_LABELS` below; `review_note` gets the same length-cap treatment
as `watch_note`/reverse-split notes (validated independently, per this
project's "each note field validated on its own" precedent, even though
the limit happens to be the same number). `review_trade` refuses to set
any of these on a trade that's still open (`exit_ts IS NULL`) -- an open
position's outcome isn't known yet, so labeling it doesn't mean
anything yet.
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
);
CREATE TABLE IF NOT EXISTS equity_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    value REAL NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS equity_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    old_value REAL,
    new_value REAL NOT NULL,
    reason TEXT NOT NULL,
    changed_at INTEGER NOT NULL
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
    ("shares", "INTEGER"),
    ("account_size_used", "REAL"),
    ("risk_pct_used", "REAL"),
    ("risk_amount_used", "REAL"),
    ("realized_pnl_dollars", "REAL"),
    ("exit_phase", "TEXT"),
    ("swing_low_buffer_pct_used", "REAL"),
    ("pattern_progress_threshold_pct_used", "REAL"),
    ("phase_transitioned_ts", "INTEGER"),
    ("review_label", "TEXT"),
    ("review_note", "TEXT"),
    ("ideal_entry_price", "REAL"),
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
# base_equity (specs.md section 7's position-sizing gap) is a dollar
# account size -- must be positive, with a generous-but-non-absurd
# ceiling matching this project's existing bounds style (nothing about
# this virtual journal needs an account size in the tens of millions).
# risk_pct_per_trade is a fraction of that account risked on a single
# entry (0.01 = 1%, the same convention the EOD swing bot used) -- 0.5
# (50%) as a ceiling mirrors trail_pct's own reasoning exactly: already
# far more than this strategy would ever plausibly risk on one trade,
# chosen as a sanity ceiling, not a validated "correct" number.
# swing_low_buffer_pct (specs.md section 12) is a small cushion below
# whatever anchor (a confirmed swing low, or the entry-trigger level)
# governs phase 1's stop -- 0.005 (0.5%) is the chosen default: enough
# to absorb a typical wick-through-the-exact-low without meaningfully
# widening the stop, small enough that it's still anchored to a REAL
# level, not a guess; 0.1 (10%) as a ceiling is already a wide cushion
# for a "small buffer" concept, a sanity bound not a validated number.
# pattern_progress_threshold_pct is how far above entry price must climb
# (measured via high_water_mark, same ratchet-from-high convention as
# the flat trail itself) before phase 1 hands off to phase 2's proven
# flat trail -- 0.03 (3%) is the chosen default: comfortably past normal
# intrabar noise/spread on these volatile low-priced candidates, while
# still handing off early enough that most real winners actually reach
# phase 2 (the point of having one at all); 1.0 (100%) as a ceiling
# reflects how volatile these candidates genuinely are, not an absurd
# extreme for this project's own trading range.
# session_volume_multiple (specs.md section 12) is how many multiples of
# a symbol's typical daily volume today's cumulative session volume
# must clear to allow an entry -- 3.0 is the user's own stated
# criterion, not a guess; 50.0 as a ceiling mirrors volume_confirm_
# threshold's own "generous but non-absurd" reasoning, scaled up since
# this compares a full day's cumulative volume, not one bar's.
# continuation_lookback_days (specs.md section 7's continuation-vs-
# fresh-day gap) is how many recent trading days get scanned for a
# qualifying move -- 7 (about a trading week) is the chosen default:
# long enough that a runner day's immediate aftermath (the "Day 2, Day
# 3..." continuation window this flag exists to distinguish from a
# genuine Day 1) is still caught, short enough that a move from weeks
# ago has stopped being relevant context for TODAY's setup; 30 (about
# six weeks) as a ceiling is already well past what "recent" means for
# this purpose. continuation_threshold_pct is how large a single day's
# move (either direction) must be to count as a qualifying day -- 0.5
# (50%) is the chosen default: real examples from this project's own
# candidates (RETO, QCLS, DLXY) moved 100%+ on their actual runner
# days, but ordinary daily noise for a volatile small/micro cap can
# itself run into the 10-30% range on an unremarkable day -- 50% sits
# meaningfully above that noise floor without requiring the most
# extreme outcomes only to register as "not a normal day"; 5.0 (500%)
# as a ceiling is a generous sanity bound (a move that large is
# essentially a halt/reopen event), not a validated "correct" number.
_PARAM_BOUNDS = {
    "trail_pct": (0.0, 0.5),
    "volume_confirm_threshold": (0.0, 20.0),
    "base_equity": (0.0, 10_000_000.0),
    "risk_pct_per_trade": (0.0, 0.5),
    "swing_low_buffer_pct": (0.0, 0.1),
    "pattern_progress_threshold_pct": (0.0, 1.0),
    "session_volume_multiple": (0.0, 50.0),
    "continuation_lookback_days": (0.0, 30.0),
    "continuation_threshold_pct": (0.0, 5.0),
    # How long a persisted confirmed=True stays actionable for a NEW
    # entry after it was last genuinely reaffirmed (phase 3.6 follow-up,
    # specs.md section 20) -- 0 would make it impossible to ever enter
    # (nothing is fresh at ts==confirmed_at_ts plus any real gap), 3600
    # (1 hour) is a generous upper bound comfortably above any
    # legitimate use, same "wide but not unbounded" pattern as the other
    # thresholds here.
    "confirmation_freshness_seconds": (0.0, 3600.0),
    # Reference-target display (informational only, specs.md section 21)
    # -- entry_price * (1 + this) shown alongside the real trailing stop,
    # never an exit trigger. 5.0 (500%) is a generous upper bound; this
    # is display-only so there's no real-risk reason to cap it tighter.
    "target_reference_pct": (0.0, 5.0),
    # Event-triggered narration's rate-limit circuit breaker (phase 3
    # stage 1, specs.md section 27) -- more than narration_max_calls_
    # per_window real `claude -p` calls within a rolling narration_
    # window_minutes trips the breaker (blocks all further calls until a
    # human manually resets it). Reasoning behind the defaults (10 calls
    # / 15 minutes): expected REAL volume is low even at 4 concurrently
    # watched symbols on a genuinely busy session -- each symbol
    # realistically produces at most a handful of the three trigger
    # events (a setup confirming, an entry, an exit) in any 15-minute
    # window, so 4-8 total calls in that window is a busy-but-normal
    # ceiling; 10 sits just above that. A real bug (e.g. a debounce
    # failure firing on every live bar at 10s cadence) would produce
    # dozens of calls per symbol in the same window -- the threshold
    # trips almost immediately against that, not after meaningful
    # damage. 1 (a pathologically tight window that would trip on any
    # single real trigger) is the floor; 1000 calls / 1440 minutes (a
    # full day) are generous sanity ceilings, not validated numbers.
    "narration_max_calls_per_window": (1.0, 1000.0),
    "narration_window_minutes": (1.0, 1440.0),
    # Mandatory hourly re-arm (independent of the circuit breaker above)
    # -- narration only fires while "armed", for a bounded rolling
    # duration from the last explicit arm/re-arm action; once expired,
    # narration goes dormant until a human explicitly re-arms it. 60
    # (the stated default) balances not needing constant babysitting
    # against never letting narration run unattended, unbounded, for an
    # entire session -- a deliberate, conservative default for a brand
    # new, unproven capability, not a validated number. 1440 (a full
    # day) is a generous sanity ceiling; 1 minute is the floor (a
    # pathologically short arm window is still a real, valid choice for
    # someone who wants to watch every single call land).
    "narration_rearm_minutes": (1.0, 1440.0),
}

# current_equity's own seed/reset fallback (specs.md section 7) -- "2000
# (the reset target)" per spec. Used when default_params carries no
# "base_equity" key at all (e.g. journal_store constructed directly in a
# test with no default_params) and by reset_equity's own get_param
# fallback -- mirrors get_param's existing "value or explicit default"
# pattern rather than ever leaving current_equity undefined.
DEFAULT_BASE_EQUITY = 2000.0


class InvalidParamError(ValueError):
    """Raised by set_param for a value outside _PARAM_BOUNDS, or a key
    with no known bounds at all (a mistyped or unsupported key)."""


class InvalidEquityOverrideError(ValueError):
    """Raised by override_equity for a non-positive value."""


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


# Human review/labeling (specs.md section 24) -- the categorization axis
# is SIGNAL QUALITY, deliberately independent of outcome (win/loss is
# already fully captured by realized_pnl_pct/exit_reason, so a label
# duplicating that would add nothing). Three categories, chosen to
# directly answer the original framing: "was this a genuine, trustworthy
# signal, or did it just happen to work out (or not) by chance?"
#   - "clean_signal": the setup was genuine and well-formed -- entry
#     criteria were legitimately met, and it reads correctly in
#     hindsight. A clean signal can still LOSE (normal variance) without
#     becoming "bad_signal" -- this label is about whether the signal
#     itself was trustworthy, not whether it happened to win.
#   - "lucky": won, but not because the signal was actually sound --
#     credits the outcome to chance rather than the setup.
#   - "bad_signal": the setup itself was flawed or questionable (chop, a
#     marginal/false confirmation, thin volume) regardless of how it
#     turned out -- a loss here is a well-deserved one, not variance.
# A trade with no review yet has review_label = NULL, distinct from any
# of the three real categories -- "not yet reviewed" must never be
# confused with a real judgment call.
REVIEW_LABELS = frozenset({"clean_signal", "lucky", "bad_signal"})

# Same length-cap treatment as MAX_WATCH_NOTE_LENGTH/MAX_REVERSE_SPLIT_
# NOTE_LENGTH -- its own constant, not reused, per this project's "each
# note field validated independently" precedent (specs.md section 8),
# even though the number happens to match.
MAX_REVIEW_NOTE_LENGTH = 500


class InvalidReviewError(ValueError):
    """Raised by review_trade for: no trade with the given id, a trade
    that's still open (exit_ts IS NULL -- its outcome isn't known yet,
    so labeling it doesn't mean anything), a review_label outside
    REVIEW_LABELS, a review_note over MAX_REVIEW_NOTE_LENGTH, or a
    non-positive ideal_entry_price."""


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
        self._seed_equity(default_params or {})
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

    def _seed_equity(self, defaults: dict[str, float]) -> None:
        """current_equity starts at base_equity's seed value, on the
        FIRST-EVER run only -- same never-reset-on-a-later-restart
        precedent as _seed_params, since main.py passes the same
        default_params in on every startup, tuned or not."""
        existing = self._conn.execute("SELECT 1 FROM equity_state WHERE id = 1").fetchone()
        if existing is None:
            base = defaults.get("base_equity", DEFAULT_BASE_EQUITY)
            self._conn.execute(
                "INSERT INTO equity_state (id, value, updated_at) VALUES (1, ?, ?)",
                (base, int(self._now_fn())),
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

    # -- compounding virtual equity (specs.md section 7) -------------------

    def current_equity(self) -> float:
        """The live running account balance a fresh entry sizes against
        -- never adjusted for unrealized/open positions (specs.md:
        sizing a new entry while others remain open uses whatever this
        was as of the last REALIZED close, full stop). Falls back to
        DEFAULT_BASE_EQUITY only in the never-expected case that
        equity_state somehow has no row yet (it's always seeded by
        __init__, same defensive style as get_param's own fallback)."""
        row = self._conn.execute("SELECT value FROM equity_state WHERE id = 1").fetchone()
        return row["value"] if row is not None else DEFAULT_BASE_EQUITY

    def _write_equity(self, old_value: float, new_value: float, reason: str) -> float:
        """The one place that actually moves current_equity -- every
        caller below (apply_realized_pnl/reset_equity/override_equity)
        routes through here, so the value+history write can never drift
        apart or happen out of order. ALWAYS called with an `old_value`
        read fresh, immediately beforehand, by the caller (never a value
        cached earlier in a batch of several updates) -- this is what
        makes two same-batch closes apply sequentially rather than one
        silently clobbering or double-counting against the other (the
        same class of bug already found once in this project, app.py's
        opened/updated/closed if/elif/elif during Part A of setup-type
        generalization, see specs.md -- guarded against here structurally,
        not just by convention)."""
        now = int(self._now_fn())
        self._conn.execute(
            "INSERT INTO equity_state (id, value, updated_at) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (new_value, now),
        )
        self._conn.execute(
            "INSERT INTO equity_history (old_value, new_value, reason, changed_at) "
            "VALUES (?, ?, ?, ?)",
            (old_value, new_value, reason, now),
        )
        self._conn.commit()
        return new_value

    def apply_realized_pnl(self, trade_id: int, pnl_dollars: float) -> float:
        """The ONLY automatic path that moves current_equity (specs.md:
        "do this and nothing else moves current_equity automatically") --
        called once per REAL trade close (never for a symbol_switched
        housekeeping force-close, see app.py's remove_symbol) with the
        real realized dollar P&L (shares * (exit_price - entry_price)).
        reason is machine-parseable (`trade_close:trade_id=<id>:
        pnl=<+/-X.XX>`) so the full equity curve is reconstructable later
        with real provenance, not just a bare sequence of numbers."""
        old = self.current_equity()
        new = old + pnl_dollars
        reason = f"trade_close:trade_id={trade_id}:pnl={pnl_dollars:+.2f}"
        return self._write_equity(old, new, reason)

    def reset_equity(self) -> float:
        """Sets current_equity to the LIVE base_equity strategy_param
        (not a frozen constant -- a reset targets whatever base_equity
        has since been tuned to, per specs.md), logged as "manual_reset"
        -- distinct from a trade-driven change, never confused with one
        later."""
        old = self.current_equity()
        base = self.get_param("base_equity", DEFAULT_BASE_EQUITY)
        return self._write_equity(old, base, "manual_reset")

    def override_equity(self, value: float) -> float:
        """Sets current_equity directly to an arbitrary value -- for
        correcting a mistake or deliberately starting from a different
        number, WITHOUT changing what a future reset targets (that's
        base_equity, untouched here). Raises InvalidEquityOverrideError
        (changing nothing) for a non-positive value; logged as
        "manual_override", distinct from "manual_reset"."""
        if value <= 0:
            raise InvalidEquityOverrideError(f"value {value!r} must be positive")
        old = self.current_equity()
        return self._write_equity(old, value, "manual_override")

    def equity_history(self, limit: int = 50) -> list[dict]:
        """Most-recent-first log of every current_equity change, with the
        provenance (`reason`) of which of the three paths produced it --
        the full equity curve is reconstructable from this, real numbers
        with real causes, not just a bare sequence."""
        rows = self._conn.execute(
            "SELECT * FROM equity_history ORDER BY id DESC LIMIT ?", (limit,),
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

    # -- human review/labeling for closed trades (specs.md section 24) ----

    def review_trade(self, trade_id: int, *, review_label: str | None = None,
                     review_note: str | None = None,
                     ideal_entry_price: float | None = None) -> None:
        """Sets or updates review_label/review_note/ideal_entry_price on
        an already-CLOSED trade -- a full replace, not a merge (there's
        no history table here, unlike watch_notes: see this module's own
        docstring for why). Re-reviewing a trade just calls this again;
        whatever the three fields were on the previous call is simply
        overwritten, never accumulated.

        Raises InvalidReviewError (changing nothing) for: no trade with
        this id, a trade that's still open (its outcome isn't known yet),
        an unrecognized review_label, an over-length review_note, or a
        non-positive ideal_entry_price. `review_label`/`review_note`/
        `ideal_entry_price` are each independently optional (None) --
        e.g. jotting a note without committing to a category yet is
        valid; None for all three is also valid (clears a prior review)."""
        row = self._conn.execute(
            "SELECT exit_ts FROM trades WHERE id = ?", (trade_id,),
        ).fetchone()
        if row is None:
            raise InvalidReviewError(f"no trade with id {trade_id}")
        if row["exit_ts"] is None:
            raise InvalidReviewError(
                f"trade {trade_id} is still open; only closed trades can be reviewed")
        if review_label is not None and review_label not in REVIEW_LABELS:
            raise InvalidReviewError(
                f"{review_label!r} is not a recognized review label "
                f"(expected one of: {', '.join(sorted(REVIEW_LABELS))})")
        if review_note is not None and len(review_note) > MAX_REVIEW_NOTE_LENGTH:
            raise InvalidReviewError(
                f"review_note is {len(review_note)} characters, over the "
                f"{MAX_REVIEW_NOTE_LENGTH}-character limit")
        if ideal_entry_price is not None and ideal_entry_price <= 0:
            raise InvalidReviewError(
                f"ideal_entry_price must be positive, got {ideal_entry_price}")
        self._conn.execute(
            "UPDATE trades SET review_label = ?, review_note = ?, "
            "ideal_entry_price = ? WHERE id = ?",
            (review_label, review_note, ideal_entry_price, trade_id),
        )
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
            "high_water_mark, stop_level, setup_type, factors, "
            "trail_pct_used, volume_threshold_used, watch_note, "
            "shares, account_size_used, risk_pct_used, risk_amount_used, "
            "exit_phase, swing_low_buffer_pct_used, "
            "pattern_progress_threshold_pct_used, phase_transitioned_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (position.symbol.upper(), position.entry_ts, position.entry_price,
             position.high_water_mark, position.stop_level, position.setup_type,
             json.dumps(position.factors) if position.factors is not None else None,
             position.trail_pct, position.volume_threshold_used, position.watch_note,
             position.shares, position.account_size_used, position.risk_pct_used,
             position.risk_amount_used, position.exit_phase,
             position.swing_low_buffer_pct_used,
             position.pattern_progress_threshold_pct_used,
             position.phase_transitioned_ts),
        )
        self._conn.commit()
        return replace(position, id=cur.lastrowid)

    def update_trailing(self, position: OpenPosition) -> None:
        # exit_phase/phase_transitioned_ts persist here too (specs.md
        # section 12) -- the ONE-WAY swing_low -> trailing transition
        # happens mid-trade, on a ratchet, not at create()/close_position()
        # time, so it needs to survive a restart the same way high_water_
        # mark/stop_level already do.
        self._conn.execute(
            "UPDATE trades SET high_water_mark = ?, stop_level = ?, "
            "exit_phase = ?, phase_transitioned_ts = ? WHERE id = ?",
            (position.high_water_mark, position.stop_level, position.exit_phase,
             position.phase_transitioned_ts, position.id),
        )
        self._conn.commit()

    def close_position(self, position: OpenPosition, exit_event: ExitEvent) -> float | None:
        """Records the exit, including realized_pnl_dollars computed from
        this position's OWN locked-in `shares` (shares * (exit_price -
        entry_price)) -- None (never an invented number) for a position
        whose shares were never computed (a pre-migration open position,
        see the migration tests). Returns that same dollar figure (or
        None) so the caller can decide whether/how to apply it to
        current_equity -- this method itself never touches current_equity;
        specs.md is explicit that only the caller's own judgment (a real
        trade exit vs. e.g. a symbol_switched housekeeping force-close,
        see app.py's remove_symbol) decides that."""
        pnl_pct = ((exit_event.exit_price - position.entry_price)
                  / position.entry_price * 100.0)
        pnl_dollars = (position.shares * (exit_event.exit_price - position.entry_price)
                      if position.shares is not None else None)
        self._conn.execute(
            "UPDATE trades SET exit_ts = ?, exit_price = ?, exit_reason = ?, "
            "realized_pnl_pct = ?, realized_pnl_dollars = ? WHERE id = ?",
            (exit_event.exit_ts, exit_event.exit_price, exit_event.exit_reason,
             pnl_pct, pnl_dollars, position.id),
        )
        self._conn.commit()
        return pnl_dollars

    def recent_closed(self, limit: int | None = 10) -> list[dict]:
        """Most recent first. `limit=None` (specs.md section 26's loss
        monitoring/evaluation view) returns EVERY closed trade, not just
        the live page's own most-recent-10 window -- an aggregate stat
        computed over only the 10 most recently displayed trades would
        silently ignore the rest of a symbol's real history the moment
        more than 10 have ever closed."""
        if limit is None:
            rows = self._conn.execute(
                "SELECT * FROM trades WHERE exit_ts IS NOT NULL "
                "ORDER BY exit_ts DESC",
            ).fetchall()
        else:
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
# The dataclass's own default ("trailing") -- a pre-migration open
# position, resumed after this feature shipped, was ALREADY using the
# original flat-trail-only mechanism the whole time it's been open;
# resuming it into "swing_low" phase would be a wrong, retroactive
# behavior change for a trade already in flight. Reading a NULL
# exit_phase column back as "trailing" (never "swing_low", the
# dataclass field default would otherwise imply nothing either way
# here, since _row_to_position always passes an explicit value) is
# what keeps that correct -- same migration discipline as trail_pct
# above, applied to a case where the safe fallback ISN'T the dataclass
# field default's own literal value.
_DEFAULT_EXIT_PHASE_ON_RESUME = "trailing"


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
        shares=row["shares"], account_size_used=row["account_size_used"],
        risk_pct_used=row["risk_pct_used"], risk_amount_used=row["risk_amount_used"],
        exit_phase=(row["exit_phase"] if row["exit_phase"] is not None
                   else _DEFAULT_EXIT_PHASE_ON_RESUME),
        swing_low_buffer_pct_used=row["swing_low_buffer_pct_used"],
        pattern_progress_threshold_pct_used=row["pattern_progress_threshold_pct_used"],
        phase_transitioned_ts=row["phase_transitioned_ts"],
    )


def _decode_factors(row: dict) -> dict:
    if row.get("factors") is not None:
        row["factors"] = json.loads(row["factors"])
    return row
