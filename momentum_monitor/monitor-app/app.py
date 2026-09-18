"""
monitor-app FastAPI web app -- phase 2: up to MAX_SYMBOLS (4) concurrently
watched symbols, each independently polled, analyzed, and journaled.

Holds no credentials. Bars now arrive by PUSH, not poll (fixed
2026-09-17 -- see specs.md: the old 5s poll of schwab-connector stacked
with the browser's old 4s poll of this service added up to ~9s of
self-inflicted latency on top of Schwab's own, separately-fixed,
throttling ceiling). schwab-connector streams every bar close over one
shared SSE connection (GET /events); main.py's stream_events() consumes
it and calls Poller.apply_bar_push() the instant one arrives, which
keeps each symbol's own running bar list and recomputes that symbol's
full state (state.build_state, UNCHANGED -- still the exact same
per-symbol math run N times, not different math). A REST catch-up
fetch (Poller.catch_up(), still using fetch_bars) still runs once when
a symbol is first added, and again for every watched symbol on
stream_events' on_reconnect callback (Poller.resync_all()) -- both
close any gap between "what monitor-app has" and "what schwab-connector
has stored", the same dedup guard `_apply_new_bars` always applied.
Serves:

  GET  /api/state    -> {"symbols": {SYM: {...same shape as phase 1's
                        whole response, plus a per-symbol "journal.open"},
                        ...}, "recent_closed": [...], "poll_enabled": bool,
                        "max_symbols": int}
                        Deliberate breaking change from phase 1's single-
                        object shape -- nothing else depends on the old
                        form, no back-compat shim. Kept for first paint
                        and tooling even now that live updates are
                        pushed -- see /api/state/stream below.
  GET  /api/state/stream -> Server-Sent Events, one `data:` line per
                        SAME-SHAPED snapshot as GET /api/state above,
                        pushed the instant Poller state changes (a new
                        bar applied, a symbol added/removed, a journal
                        row deleted) -- replaces the browser's old 4s
                        setInterval poll. Emits one immediate snapshot on
                        connect so a fresh tab doesn't wait for the next
                        change, plus a 15s keep-alive comment line while
                        idle.
  GET  /             -> Stage B: a responsive grid of up to 4 symbol
                        panels, an add-symbol form (POSTs /api/watch,
                        fills the next empty slot -- never replaces an
                        existing one), and a remove control on each panel
                        (POSTs /api/unwatch for that panel's own symbol
                        only). JS-refreshed in place the same way as
                        phase 1 (no meta-refresh, no full-page reload):
                        render(data) rebuilds #symbols' innerHTML on
                        every EventSource message (or explicit
                        post-action refresh()).
  POST /api/watch    -> {"symbol": "..."} (urlencoded form) ADDS a symbol
                        to the watched set (up to max_symbols) -- this is
                        a deliberate behavior change from phase 1, where
                        the same endpoint SWITCHED to a symbol, replacing
                        whatever was watched. JSON response now, not a
                        redirect: {"ok": bool, "reason": str, "symbols":
                        [...]}, 200 on success / 409 on rejection (already
                        watching it, or an invalid symbol -- never a
                        silent failure). Adding while already at
                        max_symbols is NOT a rejection: it evicts the
                        oldest-added symbol (FIFO, via remove_symbol --
                        same force-close-journal/announce_unwatch
                        treatment as an explicit unwatch) to make room,
                        200 with "reason" carrying a human-readable note
                        of what got dropped -- never a SILENT eviction,
                        just not a REJECTED add.
  POST /api/unwatch  -> {"symbol": "..."} removes one specific symbol,
                        force-closing any open virtual-journal position
                        for it (see Poller.remove_symbol).
  POST /api/polling  -> {"enabled": bool} pauses/resumes monitor-app's own
                        applying of incoming bar-push events for ALL
                        watched symbols at once (one global switch, not
                        per-symbol) -- see Poller.set_poll_enabled. Route
                        name kept from the poll-based design; the pause
                        semantic is unchanged, only what it gates (push
                        application instead of a poll timer) is new.
  POST /api/journal/delete -> {"id": "..."} (urlencoded form) permanently
                        deletes ONE closed trade row from the SQLite
                        journal (never an open position -- see
                        JournalStore.delete_closed). 200 {"ok": true} on
                        success, 404 {"ok": false} for an unknown id, 409
                        for a non-numeric one. The page's own JS gates the
                        call behind a confirm() dialog; this endpoint
                        trusts the caller already got that confirmation.
  POST /api/journal/clear_symbol_switched -> permanently deletes EVERY
                        symbol_switched closed row in one call (the bulk
                        "clear housekeeping noise" action, specs.md
                        section 6) -> {"ok": true, "deleted": <count>}.
                        Same confirm()-before-calling convention as the
                        per-row delete above.

create_app() takes fetch_bars / announce_watch / announce_unwatch as
callables so tests inject fakes; main.py binds them to httpx calls against
schwab-connector. journal_store (optional -- a journal_store.JournalStore)
wires in the phase-4 virtual trade journal (specs.md section 6); passing
None disables it entirely. journal_logic.py and journal_store.py needed NO
changes for phase 2 -- audited specifically for any single-global-position
assumption (per the multi-symbol design doc) and found none: journal_store
already scopes every open-position lookup by symbol (`open_position_for`)
or by the specific row id (`update_trailing`/`close_position`), never "the
one open position"; `recent_closed` is intentionally cross-symbol, per
specs.md, and stays that way. The single-position assumption that DID
exist lived in this module's own Poller (scalar `self._journal_position`
etc.), which phase 2 replaces with a `_SymbolSlot` per symbol below.

schwab-connector needed ZERO structural changes either, confirmed by
reading it (not assumed): Connector._sources/_tasks are already dicts
keyed by symbol, watch()/unwatch() already operate per-key with no cap,
and BarStore's cache is already keyed by symbol. The earlier watch/unwatch
leak (fixed pre-phase-2) was only possible BECAUSE schwab-connector was
already happily running multiple concurrent per-symbol streams with no
artificial limit -- that capability existing is exactly why this phase
needs no schwab-connector changes, per specs.md's own roadmap note.

(Superseded 2026-09-17, twice over -- see specs.md: schwab-connector now
serves every watched symbol over ONE shared Schwab stream connection,
not one per symbol as this paragraph originally described; and bars now
reach this service by push over that same shared-connection pattern
(GET /events), not by this service polling schwab-connector on a timer.)
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from journal_logic import ExitEvent, OpenPosition, advance_journal
from journal_store import (MAX_WATCH_NOTE_LENGTH, InvalidEquityOverrideError,
                           InvalidParamError, InvalidReverseSplitError,
                           InvalidWatchNoteError)
from state import build_state

# Same sys.path setup as state.py's own CORE_PATH -- app.py reaches into
# core/levels.py directly for confirmed_swing_lows (specs.md section 12's
# early-phase exit), a journal-specific need state.build_state's output
# has no reason to carry, unlike setups/relative_volume which the WHOLE
# page displays. Explicit here rather than relying on the side effect of
# state.py's own sys.path insertion having already run by this point.
_CORE = os.environ.get("CORE_PATH") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core"
)
if _CORE not in sys.path:
    sys.path.insert(0, _CORE)

from indicators import continuation_days  # noqa: E402
from levels import confirmed_swing_lows  # noqa: E402

# Same exchange-local timezone state.py already anchors session VWAP to
# (specs.md section 3) -- entry_ts/exit_ts are displayed in it too (Part D,
# 2026-09-17), for the same reason: this app has no other timezone
# convention to be consistent with.
_NY = ZoneInfo("America/New_York")

logger = logging.getLogger(__name__)

DEFAULT_TRAIL_PCT = 0.05
# How far above "average" (1.0 = equal to the trailing 20-bar volume
# average, core/indicators.py's relative_volume) a bar's volume must be,
# at the moment a setup type confirms, for the entry to actually fire
# (added 2026-09-17 -- see specs.md). 1.5x is a defensible starting
# point, not a validated number, same treatment as TRAIL_PCT: high
# enough to filter a low-conviction drift through a level (the exact
# false-breakout pattern volume confirmation exists to catch), not so
# high it requires an extreme spike that would filter out most real
# breakouts too. Applies to ENTRIES ONLY -- exits stay fast and
# unconditional, same asymmetry as always (journal_logic.py).
DEFAULT_VOLUME_CONFIRM_THRESHOLD = 1.5
# Position sizing with compounding virtual equity (specs.md section 7).
# base_equity is the dollar "reset target" a fresh journal starts from
# and a manual reset returns to; risk_pct_per_trade (1%) is the same
# convention the EOD swing bot used. Both are seed/fallback defaults
# ONLY -- see journal_store.py's DEFAULT_BASE_EQUITY and _PARAM_BOUNDS,
# same fallback-vs-live-tunable split as DEFAULT_TRAIL_PCT above.
DEFAULT_BASE_EQUITY = 2000.0
DEFAULT_RISK_PCT_PER_TRADE = 0.01
# Two-phase exit + session-level volume gate (specs.md section 12). See
# journal_store.py's _PARAM_BOUNDS comment for the full reasoning behind
# each default -- these are seed/fallback defaults ONLY, same
# fallback-vs-live-tunable split as DEFAULT_TRAIL_PCT above.
DEFAULT_SWING_LOW_BUFFER_PCT = 0.005
DEFAULT_PATTERN_PROGRESS_THRESHOLD_PCT = 0.03
DEFAULT_SESSION_VOLUME_MULTIPLE = 3.0
# How many trading days of daily history to fetch (once per symbol per
# watch, cached on _SymbolSlot.avg_daily_volume, never per-bar) for the
# session-level volume gate's baseline.
DAILY_VOLUME_LOOKBACK_DAYS = 30
# Continuation-vs-fresh-day flag (specs.md section 7) -- see
# journal_store.py's _PARAM_BOUNDS comment for the full reasoning behind
# each default; seed/fallback defaults ONLY, same split as every other
# DEFAULT_* constant here.
DEFAULT_CONTINUATION_LOOKBACK_DAYS = 7
DEFAULT_CONTINUATION_THRESHOLD_PCT = 0.5
MAX_SYMBOLS = 4

# Real ticker symbols are short and plain (letters/digits, occasionally a
# dot or hyphen for share classes). Rejecting anything else keeps a typo
# or stray input from becoming part of the path in schwab-connector's
# GET /bars/{symbol} or POST /watch requests (main.py) -- schwab-connector
# is internal-only with a small fixed route set, so this is defense in
# depth, not a hard security boundary.
_VALID_SYMBOL = re.compile(r"^[A-Z0-9.\-]{1,10}$")

# schwab-connector's own startup (even past its healthcheck-gated container
# start) can still be a beat behind uvicorn accepting connections, so the
# startup announce_watch() gets a few short, backed-off retries rather than
# one silent attempt -- a symbol that never gets registered as watched is a
# silent, permanent failure of the whole pipeline, not something to let ride.
ANNOUNCE_RETRY_ATTEMPTS = 5
ANNOUNCE_RETRY_BASE_DELAY_SECONDS = 1.0
ANNOUNCE_RETRY_MAX_DELAY_SECONDS = 8.0


@dataclass
class _SymbolSlot:
    """Everything the Poller tracks for ONE watched symbol. Phase 1 had
    these as scalar fields directly on Poller (self._bars, self._symbol,
    self._journal_position, ...) -- phase 2 needs up to MAX_SYMBOLS of
    these independently and simultaneously, so they move into their own
    per-symbol object. `journal_position`/`journal_confirmed_types` being
    per-slot (not per-Poller) is exactly what makes "at most one open
    position PER symbol" (rather than one globally) correct.

    `journal_confirmed_types` (generalized 2026-09-17 from a single
    `journal_was_confirmed: bool` -- see specs.md) tracks which of the
    four setup types are CURRENTLY confirmed, per type, not one collapsed
    boolean -- so a type confirming freshly while a DIFFERENT type is
    still sitting confirmed from earlier is still detected as its own
    entry signal (journal_logic.py's should_enter)."""
    symbol: str
    bars: list[dict] = field(default_factory=list)
    last_ts: float = 0.0
    state: dict = field(default_factory=dict)
    poll_ok: bool = False
    journal_position: OpenPosition | None = None
    journal_confirmed_types: frozenset[str] = field(default_factory=frozenset)
    # Fetched ONCE, when the symbol is first added (specs.md section 12's
    # session-level volume gate), never per-bar -- None if never fetched
    # yet, or if the fetch failed/returned nothing (too new a symbol, a
    # data gap), which SKIPS the gate for this symbol's entries rather
    # than blocking them (see journal_logic.should_enter's documented
    # choice), not a crash or a silently-wrong zero.
    avg_daily_volume: float | None = None
    # The RAW daily bars that avg_daily_volume above was computed from --
    # retained (added 2026-09-18, specs.md section 7's continuation-vs-
    # fresh-day gap), not discarded, so the continuation flag reuses this
    # SAME single fetch instead of pulling the same underlying data
    # twice. [] if never fetched yet, or the fetch failed/returned
    # nothing -- same "unknown, not silently zero" meaning as
    # avg_daily_volume being None (see _continuation_status_for).
    daily_bars: list[dict] = field(default_factory=list)


class Poller:
    def __init__(self, *, fetch_bars, watch_symbol=None, announce_watch,
                 announce_unwatch=None,
                 announce_retry_attempts=ANNOUNCE_RETRY_ATTEMPTS,
                 announce_retry_base_delay=ANNOUNCE_RETRY_BASE_DELAY_SECONDS,
                 announce_retry_max_delay=ANNOUNCE_RETRY_MAX_DELAY_SECONDS,
                 journal_store=None, trail_pct=DEFAULT_TRAIL_PCT,
                 volume_confirm_threshold=DEFAULT_VOLUME_CONFIRM_THRESHOLD,
                 base_equity=DEFAULT_BASE_EQUITY,
                 risk_pct_per_trade=DEFAULT_RISK_PCT_PER_TRADE,
                 swing_low_buffer_pct=DEFAULT_SWING_LOW_BUFFER_PCT,
                 pattern_progress_threshold_pct=DEFAULT_PATTERN_PROGRESS_THRESHOLD_PCT,
                 session_volume_multiple=DEFAULT_SESSION_VOLUME_MULTIPLE,
                 continuation_lookback_days=DEFAULT_CONTINUATION_LOOKBACK_DAYS,
                 continuation_threshold_pct=DEFAULT_CONTINUATION_THRESHOLD_PCT,
                 fetch_daily_bars=None,
                 now_fn=time.time, max_symbols=MAX_SYMBOLS):
        self._fetch_bars = fetch_bars
        self._fetch_daily_bars = fetch_daily_bars
        self._initial_symbol = watch_symbol.upper() if watch_symbol else None
        self._announce_watch = announce_watch
        self._announce_unwatch = announce_unwatch
        self._announce_retry_attempts = announce_retry_attempts
        self._announce_retry_base_delay = announce_retry_base_delay
        self._announce_retry_max_delay = announce_retry_max_delay
        self._journal_store = journal_store
        self._trail_pct = trail_pct
        self._volume_confirm_threshold = volume_confirm_threshold
        self._base_equity = base_equity
        self._risk_pct_per_trade = risk_pct_per_trade
        self._swing_low_buffer_pct = swing_low_buffer_pct
        self._pattern_progress_threshold_pct = pattern_progress_threshold_pct
        self._session_volume_multiple = session_volume_multiple
        self._continuation_lookback_days = continuation_lookback_days
        self._continuation_threshold_pct = continuation_threshold_pct
        self._now_fn = now_fn
        self._max_symbols = max_symbols
        self._slots: dict[str, _SymbolSlot] = {}
        self._poll_enabled = True
        self._state_subscribers: set[asyncio.Queue] = set()

    @property
    def symbols(self) -> list[str]:
        """Currently-watched symbols, in the order they were added."""
        return list(self._slots.keys())

    @property
    def max_symbols(self) -> int:
        return self._max_symbols

    @property
    def poll_enabled(self) -> bool:
        return self._poll_enabled

    async def set_poll_enabled(self, enabled: bool) -> None:
        """Pause/resume applying incoming bar-push events for ALL watched
        symbols at once (one global switch, not per-symbol) --
        schwab-connector's live stream and stored bars are unaffected
        either way, this only stops monitor-app from applying new ones
        in while nobody's watching. Distinct from (and the thing that
        actually matters, unlike) the client-side EventSource the page
        also has -- that only stops browser<->monitor-app traffic, which
        never left localhost/the LAN in the first place. Same semantic as
        the original poll-based design, just re-triggered by push instead
        of a timer: on resume, resync_all() catches up on anything that
        arrived (and was dropped) while paused, so nothing pushed during
        a pause is silently lost."""
        self._poll_enabled = enabled
        if enabled:
            await self.resync_all()

    def state_for(self, symbol: str) -> dict | None:
        slot = self._slots.get(symbol.upper())
        return slot.state if slot else None

    def full_state_for(self, symbol: str) -> dict | None:
        """state.build_state's output for `symbol`, plus that symbol's OWN
        journal open-position block and current watch_note (specs.md
        section 7 -- the live current reason, not a trade's own frozen
        snapshot; see journal_logic.OpenPosition.watch_note for that).
        Does NOT include recent_closed, which is intentionally
        cross-symbol -- see recent_closed()."""
        slot = self._slots.get(symbol.upper())
        if slot is None:
            return None
        payload = dict(slot.state)
        payload["journal"] = {"open": self._journal_open_for(slot)}
        payload["watch_note"] = (self._journal_store.current_note_for(symbol)
                                 if self._journal_store is not None else None)
        payload["reverse_splits"] = self.reverse_splits_for(symbol)
        # The session-level volume gate's cached baseline (specs.md
        # section 12) -- None means "never fetched" or "fetch failed",
        # not zero; exposed here mainly so it's directly checkable via
        # GET /api/state, same as everything else on this page.
        payload["avg_daily_volume"] = slot.avg_daily_volume
        # Continuation-vs-fresh-day flag (specs.md section 7) --
        # INFORMATIONAL only, never gates or influences entry logic (see
        # journal_logic.should_enter, which takes no continuation-related
        # parameter at all). Recomputed fresh on every read against the
        # CURRENT live threshold/lookback, not cached or snapshotted --
        # unlike avg_daily_volume, this has no reason to reflect a value
        # frozen at add-time.
        payload["continuation"] = self._continuation_status_for(slot)
        return payload

    def _continuation_status_for(self, slot: _SymbolSlot) -> dict:
        """{"status": "unknown"|"fresh"|"continuation", "days": [...],
        "lookback_days_used", "threshold_pct_used"} -- "unknown" (never
        "fresh") when slot.daily_bars is empty (the fetch never
        happened, or failed): "no qualifying day found" and "no data to
        check" are different facts, and must never be displayed as the
        same thing (specs.md section 7's own framing: "unknown, no
        data" must never be confused with "checked, and it's fresh")."""
        if not slot.daily_bars:
            return {"status": "unknown", "days": [],
                   "lookback_days_used": None, "threshold_pct_used": None}
        lookback_days = (self._journal_store.get_param(
            "continuation_lookback_days", self._continuation_lookback_days)
            if self._journal_store is not None else self._continuation_lookback_days)
        threshold_pct = (self._journal_store.get_param(
            "continuation_threshold_pct", self._continuation_threshold_pct)
            if self._journal_store is not None else self._continuation_threshold_pct)
        days = continuation_days(slot.daily_bars, lookback_days=int(lookback_days),
                                 threshold_pct=threshold_pct)
        return {
            "status": "continuation" if days else "fresh",
            "days": days,
            "lookback_days_used": int(lookback_days),
            "threshold_pct_used": threshold_pct,
        }

    def reverse_splits_for(self, symbol: str) -> list[dict]:
        """Every recorded reverse split for `symbol`, most recent first --
        [] if journaling is disabled or none were ever recorded. Works for
        a symbol not currently watched too (specs.md section 7: checkable
        BEFORE deciding to watch something, not only after)."""
        if self._journal_store is None:
            return []
        return self._journal_store.reverse_splits_for(symbol)

    def add_reverse_split(self, symbol: str, split_date: str, ratio: str,
                          note: str | None = None) -> tuple[bool, str]:
        """Records a reverse-split event for `symbol` (specs.md section 7)
        -- a curated, manually-entered flag, not sourced live from Schwab
        (no such data is available there). Does NOT require `symbol` to be
        currently watched. Returns (False, reason) for a blank symbol or
        journaling disabled, or (propagated from journal_store)
        InvalidReverseSplitError's message for a blank/non-ISO split_date,
        blank ratio, or over-length note -- same 409 convention as every
        other validation failure in this app."""
        symbol = symbol.strip().upper()
        if not symbol:
            return False, "symbol is required"
        if self._journal_store is None:
            return False, "journaling is disabled"
        try:
            self._journal_store.add_reverse_split(symbol, split_date, ratio, note)
        except InvalidReverseSplitError as exc:
            return False, str(exc)
        if symbol in self._slots:
            self._broadcast_state()
        return True, ""

    def all_full_states(self) -> dict[str, dict]:
        return {sym: self.full_state_for(sym) for sym in self._slots}

    def recent_closed(self, limit: int = 10) -> list[dict]:
        """Closed trades across ALL symbols, most recent first -- specs.md:
        "for end-of-day review", never scoped to just one symbol, even now
        that there can be up to `max_symbols` watched at once. Unaudited-
        assumption risk was here in a DIFFERENT sense than usual: the risk
        wasn't that this accidentally scopes to one symbol, it's confirming
        it was ALREADY correctly cross-symbol and should stay that way."""
        if self._journal_store is None:
            return []
        return self._journal_store.recent_closed(limit=limit)

    def delete_closed_trade(self, trade_id: int) -> bool:
        """Permanently deletes one closed trade row. False (a no-op) if
        journaling is disabled or no closed row with that id exists --
        see JournalStore.delete_closed for why this can never touch an
        open position."""
        if self._journal_store is None:
            return False
        deleted = self._journal_store.delete_closed(trade_id)
        if deleted:
            self._broadcast_state()
        return deleted

    def clear_symbol_switched(self) -> int:
        """Permanently deletes every symbol_switched closed row (the bulk
        housekeeping-cleanup action, specs.md section 6). Returns the
        number removed."""
        if self._journal_store is None:
            return 0
        deleted = self._journal_store.delete_symbol_switched()
        if deleted:
            self._broadcast_state()
        return deleted

    def strategy_params(self) -> dict:
        """Live-tunable strategy_params (specs.md section 8): current
        value + when each was last changed, for GET /api/strategy_params.
        Falls back to the constructor-provided defaults (never seeded/
        journaling disabled) rather than an empty dict, so the endpoint
        still reports something meaningful."""
        if self._journal_store is None:
            return {
                "trail_pct": {"value": self._trail_pct, "updated_at": None},
                "volume_confirm_threshold": {
                    "value": self._volume_confirm_threshold, "updated_at": None},
                "base_equity": {"value": self._base_equity, "updated_at": None},
                "risk_pct_per_trade": {
                    "value": self._risk_pct_per_trade, "updated_at": None},
                "swing_low_buffer_pct": {
                    "value": self._swing_low_buffer_pct, "updated_at": None},
                "pattern_progress_threshold_pct": {
                    "value": self._pattern_progress_threshold_pct, "updated_at": None},
                "session_volume_multiple": {
                    "value": self._session_volume_multiple, "updated_at": None},
                "continuation_lookback_days": {
                    "value": self._continuation_lookback_days, "updated_at": None},
                "continuation_threshold_pct": {
                    "value": self._continuation_threshold_pct, "updated_at": None},
            }
        return self._journal_store.all_params()

    def set_strategy_param(self, key: str, value: float) -> None:
        """Raises journal_store.InvalidParamError (propagated, not
        swallowed) for an invalid key or out-of-range value -- the route
        translates that into a 409, same convention as every other
        validation failure in this app. A no-op (not an error) if
        journaling is disabled entirely, matching every other
        journal_store-backed method's None-store handling."""
        if self._journal_store is None:
            return
        self._journal_store.set_param(key, value)
        self._broadcast_state()

    def strategy_param_history(self, key: str | None = None, limit: int = 50) -> list[dict]:
        if self._journal_store is None:
            return []
        return self._journal_store.param_history(key, limit)

    def current_equity(self) -> float:
        """The live virtual account balance (specs.md section 7) --
        falls back to the constructor-provided base_equity default when
        journaling is disabled, same "still report something meaningful"
        precedent as strategy_params()'s own no-store branch."""
        if self._journal_store is None:
            return self._base_equity
        return self._journal_store.current_equity()

    def equity_history(self, limit: int = 50) -> list[dict]:
        if self._journal_store is None:
            return []
        return self._journal_store.equity_history(limit)

    def reset_equity(self) -> tuple[bool, float | str]:
        """Sets current_equity to the live base_equity param, logged
        distinctly from a trade-driven change (specs.md section 7).
        Returns (False, reason) if journaling is disabled; (True,
        new_value) on success."""
        if self._journal_store is None:
            return False, "journaling is disabled"
        new_value = self._journal_store.reset_equity()
        self._broadcast_state()
        return True, new_value

    def override_equity(self, value: float) -> tuple[bool, str]:
        """Sets current_equity directly, without touching base_equity
        (specs.md section 7) -- (False, reason) for journaling disabled
        or (propagated) InvalidEquityOverrideError's message for a
        non-positive value; (True, "") on success."""
        if self._journal_store is None:
            return False, "journaling is disabled"
        try:
            self._journal_store.override_equity(value)
        except InvalidEquityOverrideError as exc:
            return False, str(exc)
        self._broadcast_state()
        return True, ""

    def _journal_open_for(self, slot: _SymbolSlot) -> dict | None:
        if slot.journal_position is None:
            return None
        pos = slot.journal_position
        last_price = slot.bars[-1]["close"] if slot.bars else pos.entry_price
        unrealized_pct = (last_price - pos.entry_price) / pos.entry_price * 100.0
        # shares/unrealized $ (specs.md section 7): real dollar P&L, now
        # that a real share count exists -- None (never a fabricated 0)
        # for a pre-migration position whose shares were never computed.
        unrealized_dollars = (pos.shares * (last_price - pos.entry_price)
                              if pos.shares is not None else None)
        return {
            "symbol": pos.symbol,
            "entry_price": round(pos.entry_price, 4),
            "stop_level": round(pos.stop_level, 4),
            "unrealized_pnl_pct": round(unrealized_pct, 4),
            "shares": pos.shares,
            "unrealized_pnl_dollars": (round(unrealized_dollars, 2)
                                       if unrealized_dollars is not None else None),
            # Two-phase exit (specs.md section 12) -- which mechanism
            # currently governs this position's own stop_level above.
            "exit_phase": pos.exit_phase,
        }

    async def run(self) -> None:
        """No longer a polling loop (fixed 2026-09-17 -- see specs.md):
        seeds the STARTING symbol once, if configured, then returns. New
        bars now arrive via apply_bar_push(), driven by main.py's
        stream_events() consuming schwab-connector's shared SSE
        connection, not by this method looping on a timer."""
        if self._initial_symbol is not None:
            await self.add_symbol(self._initial_symbol)

    async def add_symbol(self, symbol: str, watch_note: str | None = None) -> tuple[bool, str]:
        """Add a symbol to the watched set. Returns (True, note) on
        success -- `note` is "" normally, or a human-readable line saying
        what got evicted when the set was already full. Returns (False,
        reason) on rejection -- an invalid symbol, one already watched,
        or (added 2026-09-18) an over-length `watch_note` -- each with
        its own clear reason. Never a silent failure (phase 1's
        switch_symbol silently replaced whatever was watched; phase 2
        never does that for an UNRELATED slot).

        `watch_note` (specs.md section 7's highest-priority gap) is the
        catalyst/context for watching this symbol NOW -- recording it is
        part of the SAME action as adding the symbol, not a second step,
        so it's accepted right here rather than requiring a follow-up
        call. None/empty is valid and normal and never blocks or slows
        down the add; only an over-length note is rejected (before
        anything else happens -- validated up front, same as the symbol
        itself, not after the slot's already been created).

        Adding at capacity does NOT reject anymore: it evicts the
        oldest-added symbol (FIFO -- self._slots is insertion-ordered,
        same fact `symbols` relies on) via remove_symbol(), so the evicted
        symbol gets the exact same treatment as an explicit unwatch (its
        open journal position force-closed, schwab-connector told to stop
        streaming it) rather than a silent leak."""
        symbol = symbol.strip().upper()
        if not symbol or not _VALID_SYMBOL.match(symbol):
            return False, f"{symbol!r} is not a valid ticker symbol"
        if symbol in self._slots:
            return False, f"{symbol} is already being watched"
        if watch_note and len(watch_note) > MAX_WATCH_NOTE_LENGTH:
            return False, (f"note is {len(watch_note)} characters, over the "
                           f"{MAX_WATCH_NOTE_LENGTH}-character limit")

        note = ""
        if len(self._slots) >= self._max_symbols:
            oldest = next(iter(self._slots))
            await self.remove_symbol(oldest)
            note = f"dropped {oldest} (oldest) to make room for {symbol}"

        self._slots[symbol] = _SymbolSlot(symbol=symbol, state=build_state([], symbol))
        if self._fetch_daily_bars is not None:
            # Fetched ONCE per symbol, right here at add-time, never
            # per-bar (specs.md section 12) -- a failure (too new a
            # symbol, a data gap) SKIPS the session-level volume gate for
            # this symbol rather than blocking the add or every future
            # entry, same non-fatal-backfill precedent as catch_up's own
            # error handling below, never a silent crash.
            try:
                daily_bars = await self._fetch_daily_bars(symbol)
            except Exception as exc:
                logger.warning(
                    "daily-volume-history fetch failed for %s; the session-"
                    "level volume gate will be skipped for this symbol's "
                    "entries: %s", symbol, exc,
                )
                daily_bars = []
            if daily_bars:
                self._slots[symbol].avg_daily_volume = (
                    sum(b["volume"] for b in daily_bars) / len(daily_bars))
                # Retained for the continuation-vs-fresh-day flag (specs.md
                # section 7) -- REUSING this same fetch, not a second pull
                # of the same underlying data.
                self._slots[symbol].daily_bars = daily_bars
        if self._journal_store is not None and watch_note:
            self._journal_store.add_watch_note(symbol, watch_note)
        if self._journal_store is not None:
            # Resume an already-open position for this symbol (a restart,
            # or re-adding something with a position still open) rather
            # than losing track of it. Seeding journal_confirmed_types to
            # {resumed.setup_type} (the one type that actually opened it)
            # rather than every type prevents a spurious duplicate-entry
            # attempt on the very next poll for THAT type, while still
            # leaving every OTHER type free to fire its own fresh entry
            # once this position closes -- entry stays blocked either way
            # while this position is open (should_enter's position_open
            # guard), this only matters for what's "already seen" once it
            # closes.
            resumed = self._journal_store.open_position_for(symbol)
            self._slots[symbol].journal_position = resumed
            if resumed is not None and resumed.setup_type is not None:
                self._slots[symbol].journal_confirmed_types = frozenset({resumed.setup_type})
        if self._announce_watch is not None:
            await self._announce_watch_with_retry(symbol)
        if self._poll_enabled:
            # Same gating as apply_bar_push: paused means monitor-app
            # doesn't ingest anything, including a brand-new symbol's
            # initial history -- it picks up on the next resync_all()
            # when polling resumes, same as the old poll-based design
            # left a newly-added symbol for the next timer tick.
            await self.catch_up(symbol)
        return True, note

    async def remove_symbol(self, symbol: str) -> bool:
        """Stop watching a symbol. Force-closes any open virtual-journal
        position for it at its last known price, exit_reason=
        "symbol_switched" -- same meaning as phase 1's single-symbol
        switch: watching stopped, so the position could never resolve if
        left open. Returns False (a no-op, not an error) if the symbol
        wasn't being watched."""
        symbol = symbol.strip().upper()
        slot = self._slots.pop(symbol, None)
        if slot is None:
            return False
        if self._journal_store is not None and slot.journal_position is not None:
            last_bar = slot.bars[-1] if slot.bars else None
            exit_price = (last_bar["close"] if last_bar is not None
                         else slot.journal_position.entry_price)
            exit_ts = last_bar["ts"] if last_bar is not None else int(self._now_fn())
            self._journal_store.close_position(
                slot.journal_position,
                ExitEvent(exit_ts=exit_ts, exit_price=exit_price,
                         exit_reason="symbol_switched"),
            )
        if self._announce_unwatch is not None:
            # Best-effort, unlike announce_watch's retries: a failure here
            # just leaves one stale symbol watched on schwab-connector
            # (untidy, not broken), so it isn't worth delaying the removal
            # the caller is actively waiting on.
            try:
                await self._announce_unwatch(symbol)
            except Exception as exc:
                logger.warning(
                    "announce_unwatch(%s) failed; schwab-connector will keep "
                    "watching it until explicitly unwatched again: %s",
                    symbol, exc,
                )
        self._broadcast_state()
        return True

    def update_watch_note(self, symbol: str, note: str) -> tuple[bool, str]:
        """Updates the note for an ALREADY-watched symbol without
        removing/re-adding it (specs.md section 7) -- context often
        becomes clearer a minute or two after the initial add, and this
        is how that gets recorded without losing watch state. Returns
        (False, reason) if the symbol isn't currently watched (there's
        nothing to attach the note to -- use POST /api/watch instead) or
        journaling is disabled entirely; (False, reason) for an
        over-length note, same limit and message as add_symbol's own
        check. Appends a NEW watch_notes row (journal_store.
        add_watch_note), same as at add-time -- never overwrites the
        prior note in place."""
        symbol = symbol.strip().upper()
        if symbol not in self._slots:
            return False, f"{symbol} is not currently watched"
        if self._journal_store is None:
            return False, "journaling is disabled"
        try:
            self._journal_store.add_watch_note(symbol, note)
        except InvalidWatchNoteError as exc:
            return False, str(exc)
        self._broadcast_state()
        return True, ""

    async def _announce_watch_with_retry(self, symbol: str) -> None:
        delay = self._announce_retry_base_delay
        for attempt in range(1, self._announce_retry_attempts + 1):
            try:
                await self._announce_watch(symbol)
                return
            except Exception as exc:
                if attempt < self._announce_retry_attempts:
                    logger.warning(
                        "announce_watch(%s) failed on attempt %d/%d, "
                        "retrying in %.1fs: %s",
                        symbol, attempt, self._announce_retry_attempts, delay, exc,
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, self._announce_retry_max_delay)
                else:
                    logger.error(
                        "announce_watch(%s) failed on all %d attempts; "
                        "schwab-connector will never register this symbol "
                        "as watched unless something else calls POST /watch: %s",
                        symbol, self._announce_retry_attempts, exc,
                    )

    def _apply_new_bars(self, symbol: str, slot: _SymbolSlot, incoming: list[dict]) -> None:
        """Shared core for both ingestion paths: catch_up()'s REST fetch
        and apply_bar_push()'s single pushed bar. The ts-dedup guard
        below is what makes it safe to call this with overlapping data
        from either path in either order -- a bar already appended (by
        push) is silently skipped when catch_up's REST fetch later
        includes it too, and vice versa."""
        new_bars = []
        for bar in incoming:
            if not slot.bars or bar["ts"] > slot.bars[-1]["ts"]:
                slot.bars.append(bar)
                new_bars.append(bar)
        if new_bars:
            slot.last_ts = slot.bars[-1]["ts"]
        slot.state = build_state(slot.bars, symbol)
        slot.poll_ok = True
        self._update_journal(symbol, slot, new_bars)
        self._broadcast_state()

    async def catch_up(self, symbol: str) -> None:
        """One-shot REST backfill via the existing fetch_bars/GET
        /bars/{symbol} -- called once when a symbol is first added, and
        again for every watched symbol by resync_all() (see
        set_poll_enabled and main.py's stream_events' on_reconnect
        callback). Any overlap with concurrently-arriving pushed bars is
        handled by _apply_new_bars' dedup guard, so subscribing to the
        push stream first and catching up after (rather than the other
        way around) can never lose or duplicate a bar."""
        slot = self._slots.get(symbol)
        if slot is None:
            return
        try:
            incoming = await self._fetch_bars(symbol, slot.last_ts)
        except Exception:
            slot.poll_ok = False  # keep serving the last good state
            return
        if self._slots.get(symbol) is not slot:
            # remove_symbol() (possibly followed by a fresh add_symbol())
            # landed while this fetch was in flight -- identity-checking
            # the slot object, not just the key, catches BOTH "removed"
            # (get returns None) and "removed then re-added" (a NEW slot
            # object) cases. Same class of race phase 1's switch_symbol
            # guarded against, generalized to per-symbol slots.
            return
        self._apply_new_bars(symbol, slot, incoming)

    async def resync_all(self) -> None:
        """Catches up every watched symbol -- called on every
        stream_events (re)connect (closes the gap for both "the shared
        push connection wasn't up yet" and "it just dropped and
        reconnected") and on poll resume (closes the gap for whatever
        arrived, and was dropped, while paused)."""
        for symbol in list(self._slots):
            await self.catch_up(symbol)

    async def apply_bar_push(self, symbol: str, bar: dict) -> None:
        """Entry point for a bar pushed over schwab-connector's shared
        SSE connection (main.py's stream_events -> here). Gated by
        poll_enabled exactly like catch_up: paused means don't apply
        incoming updates at all -- schwab-connector keeps streaming and
        storing regardless either way, same meaning this flag has always
        had, just re-triggered by push instead of a timer tick."""
        if not self._poll_enabled:
            return
        symbol = symbol.upper()
        slot = self._slots.get(symbol)
        if slot is None:
            return  # not (or no longer) watched by this instance
        self._apply_new_bars(symbol, slot, [bar])

    @asynccontextmanager
    async def subscribe_state(self):
        """One queue per open GET /api/state/stream browser connection.
        maxsize=1, overwrite-latest (not schwab-connector's Connector.
        subscribe's drop-oldest) -- correct here specifically because
        every payload is a full snapshot, not a delta, so only the
        newest one a slow browser hasn't read yet still matters."""
        q: asyncio.Queue = asyncio.Queue(maxsize=1)
        self._state_subscribers.add(q)
        try:
            yield q
        finally:
            self._state_subscribers.discard(q)

    def _state_payload(self) -> dict:
        return {
            "symbols": self.all_full_states(),
            "recent_closed": self.recent_closed(limit=10),
            "poll_enabled": self.poll_enabled,
            "max_symbols": self.max_symbols,
            "strategy_params": self.strategy_params(),
            "current_equity": self.current_equity(),
        }

    def _broadcast_state(self) -> None:
        payload = self._state_payload()
        for q in list(self._state_subscribers):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(payload)
                except asyncio.QueueFull:
                    pass

    def _update_journal(self, symbol: str, slot: _SymbolSlot, new_bars: list[dict]) -> None:
        if self._journal_store is None:
            return
        if slot.journal_position is not None:
            # A resumed position (a restart, or re-adding a symbol with
            # one still open) can have new_bars containing history from
            # BEFORE its entry -- last_ts resets to 0.0 when a slot is
            # freshly created, so the full session gets refetched as if it
            # were all new. Exit-checking must never see pre-entry bars as
            # if they happened after entry (a real bug this caught in
            # phase 1: a resumed position was being phantom-stopped-out
            # against its own pre-entry price history on the next poll).
            new_bars = [b for b in new_bars if b["ts"] > slot.journal_position.entry_ts]
        # Generalized 2026-09-17 (see specs.md and journal_logic.py's
        # module docstring): entry now fires on ANY of the four setup
        # types' own confirmation, not just resistance's -- the full
        # setups list (already sorted closest-first, per
        # setup_types.evaluate_setups' own contract) goes to
        # advance_journal instead of a single resistance-only boolean.
        # relative_volume gates entries only (Part B) -- exits never see
        # it (apply_bar_to_open_position takes no volume argument at
        # all).
        setups = slot.state.get("setups", [])
        relative_volume = slot.state.get("session", {}).get("relative_volume", 0.0)
        # Live-tunable (2026-09-18, specs.md section 8): read from
        # strategy_params on EVERY decision, not the constructor-frozen
        # self._trail_pct/self._volume_confirm_threshold (which now only
        # serve as the seed/fallback default, passed as get_param's
        # `default` -- used if journal_store somehow has no row yet). A
        # change made via POST /api/strategy_params takes effect on the
        # very next call here, no restart needed. An already-open
        # position is unaffected either way -- advance_journal only
        # applies these to a BRAND NEW entry; an existing position keeps
        # ratcheting on its own locked-in OpenPosition.trail_pct.
        trail_pct = self._journal_store.get_param("trail_pct", self._trail_pct)
        volume_confirm_threshold = self._journal_store.get_param(
            "volume_confirm_threshold", self._volume_confirm_threshold)
        # specs.md section 7: whatever note is CURRENT for this symbol
        # right now gets snapshotted onto a fresh entry (advance_journal
        # only actually uses this when tick.opened fires) -- same
        # live-lookup-then-lock pattern as trail_pct/volume_confirm_
        # threshold above, not a live reference kept on the position.
        watch_note = self._journal_store.current_note_for(symbol)
        # Position sizing (specs.md section 7): current_equity/
        # risk_pct_per_trade read fresh from the store on EVERY decision,
        # same live-lookup-then-lock discipline as trail_pct/watch_note
        # above -- advance_journal only actually uses these when a BRAND
        # NEW entry fires, and locks them onto that position permanently.
        risk_pct_per_trade = self._journal_store.get_param(
            "risk_pct_per_trade", self._risk_pct_per_trade)
        current_equity = self._journal_store.current_equity()
        # Two-phase exit (specs.md section 12): same live-lookup-then-lock
        # discipline as every param above -- locked onto a BRAND NEW
        # entry only, never re-read for an already-open position.
        swing_low_buffer_pct = self._journal_store.get_param(
            "swing_low_buffer_pct", self._swing_low_buffer_pct)
        pattern_progress_threshold_pct = self._journal_store.get_param(
            "pattern_progress_threshold_pct", self._pattern_progress_threshold_pct)
        # The swing-low anchor an ALREADY-OPEN position's own ratchet
        # uses this cycle -- the LOWEST confirmed swing low across the
        # position's full bar history since its own entry (core/levels.
        # confirmed_swing_lows, reused not reimplemented; taking the
        # running MINIMUM across a set that only grows is what makes
        # "never moves up" true with no extra comparison needed, see
        # journal_logic.apply_bar_to_open_position). Computed ONLY when
        # still in phase 1 -- once transitioned to "trailing", this data
        # is moot and not worth computing every cycle. None (falls back
        # to the entry-trigger anchor inside journal_logic) until a
        # first swing low actually confirms.
        swing_low_anchor = None
        if (slot.journal_position is not None
                and slot.journal_position.exit_phase == "swing_low"):
            bars_since_entry = [b for b in slot.bars
                                if b["ts"] > slot.journal_position.entry_ts]
            confirmed = confirmed_swing_lows(bars_since_entry)
            if confirmed:
                swing_low_anchor = min(c["price"] for c in confirmed)
        # Session-level volume gate (specs.md section 12) -- separate
        # from, and stacking with, relative_volume above. avg_daily_volume
        # is the ONE-TIME, add-time fetch cached on this slot (never
        # re-fetched per bar) -- None (skip the gate) if it was never
        # fetched or the fetch failed, see add_symbol.
        session_volume_multiple = self._journal_store.get_param(
            "session_volume_multiple", self._session_volume_multiple)
        session_cumulative_volume = slot.state.get("session", {}).get(
            "cumulative_volume", 0.0)
        avg_daily_volume = slot.avg_daily_volume
        tick = advance_journal(
            position=slot.journal_position, new_bars=new_bars,
            setups=setups, was_confirmed_types=slot.journal_confirmed_types,
            relative_volume=relative_volume,
            volume_confirm_threshold=volume_confirm_threshold,
            trail_pct=trail_pct, symbol=symbol, watch_note=watch_note,
            current_equity=current_equity, risk_pct_per_trade=risk_pct_per_trade,
            swing_low_buffer_pct=swing_low_buffer_pct,
            pattern_progress_threshold_pct=pattern_progress_threshold_pct,
            swing_low_anchor=swing_low_anchor,
            session_cumulative_volume=session_cumulative_volume,
            avg_daily_volume=avg_daily_volume,
            session_volume_multiple=session_volume_multiple,
        )
        # tick.closed applies independently of opened/updated -- a stop-out
        # can be immediately followed, within the SAME batch of new_bars,
        # by a fresh entry on a newly-confirmed type (found via the
        # generalized entry logic above: round_number_reclaim can re-
        # confirm on the very next round-number grid point right after a
        # stop-out). opened/updated stay mutually exclusive by construction
        # (journal_logic.advance_journal never sets both), but closed+opened
        # together is a real, valid shape this must not silently drop the
        # close half of.
        if tick.closed is not None:
            position, exit_event = tick.closed
            pnl_dollars = self._journal_store.close_position(position, exit_event)
            # The ONLY automatic path that compounds current_equity
            # (specs.md section 7) -- a REAL trade exit, never the
            # symbol_switched housekeeping force-close in remove_symbol
            # below, which calls close_position directly and deliberately
            # does not reach this line. pnl_dollars is None (skip, don't
            # apply a fabricated 0) only for a pre-migration position
            # whose shares were never computed; a genuine zero-share
            # trade's pnl_dollars is 0.0, which DOES apply (a real,
            # well-defined no-op change, still logged with real
            # provenance).
            if pnl_dollars is not None:
                self._journal_store.apply_realized_pnl(position.id, pnl_dollars)
            slot.journal_position = None
        if tick.opened is not None:
            slot.journal_position = self._journal_store.create(tick.opened)
        elif tick.updated is not None:
            self._journal_store.update_trailing(tick.updated)
            slot.journal_position = tick.updated
        slot.journal_confirmed_types = tick.confirmed_types_after


# -- rendering helpers shared in spirit (deliberately, minimally
# duplicated -- see _wrap's script block) between the Python first-paint
# render below and the client-side JS that updates the same elements in
# place on every poll after that. "Single file, no framework, no build
# step" (this session's explicit constraint) rules out sharing one
# template between server and client, so a small, mirrored pair of
# renderers -- kept short and named identically in spirit on both sides --
# is the deliberate tradeoff, not an oversight.

def _fmt(v, decimals: int = 4) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{decimals}f}"
    return html.escape(str(v))


def _fmt_dollars(v) -> str:
    if v is None:
        return "—"
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}"


def _fmt_ts(ts) -> str:
    """Human-readable, America/New_York (Part D, 2026-09-17 -- entry_ts/
    exit_ts existed in the schema since phase 4 but were never shown, raw
    epoch is never displayed)."""
    if ts is None:
        return "—"
    return datetime.fromtimestamp(ts, _NY).strftime("%m/%d %H:%M:%S")


def _fmt_date(ts) -> str:
    """Date only, America/New_York -- for a DAILY bar's ts (specs.md
    section 7's continuation flag), where a time-of-day would be noise:
    a daily candle's ts isn't a moment worth showing to the minute the
    way an intraday entry_ts/exit_ts is."""
    if ts is None:
        return "—"
    return datetime.fromtimestamp(ts, _NY).strftime("%m/%d")


def _sign_class(v) -> str:
    if v is None:
        return ""
    return "pos" if v >= 0 else "neg"


def _cmp_class(a, b) -> str:
    """Class for `a` relative to reference `b` (e.g. price vs VWAP) --
    'pos' at or above, 'neg' below. Comparison-based, not a sign check."""
    if a is None or b is None:
        return ""
    return "pos" if a >= b else "neg"


_SETUP_TYPE_LABELS = {
    "resistance_breakout": "Resistance breakout",
    "micro_breakout": "Micro-breakout",
    "vwap_reclaim": "VWAP pullback-reclaim",
    "round_number_reclaim": "Round-number reclaim",
}


def _humanize_key(key: str) -> str:
    return key.replace("_", " ")


def _setup_hold_and_factor_rows_html(setup: dict) -> str:
    """Hold-state rows (same shape/labels as _level_block_html's) followed
    by that setup type's OWN factors, rendered generically from whatever
    keys core/setup_types.py put in `factors` -- one renderer shared by
    all four types rather than four hand-written layouts, since "don't
    collapse into a score" only requires showing each factor separately,
    not a bespoke table per type."""
    h = setup["hold"]
    badge = (
        "<span class='badge badge-confirmed'>confirmed</span>" if h["confirmed"]
        else "<span class='badge badge-pending'>not confirmed</span>"
    )
    rows = [
        f"<tr><th>hold direction</th><td>{html.escape(h['direction'])}</td></tr>",
        f"<tr><th>consecutive closes</th><td>{h['consecutive_bars']} / {h['required_bars']}</td></tr>",
        f"<tr><th>hold confirmed</th><td>{badge}</td></tr>",
        f"<tr><th>failed attempts</th><td>{h['failed_attempts']}</td></tr>",
    ]
    for key, value in setup["factors"].items():
        if isinstance(value, bool):
            v = "yes" if value else "no"
        elif isinstance(value, float):
            v = _fmt(value, 4)
        else:
            v = html.escape(str(value))
        rows.append(f"<tr><th>{html.escape(_humanize_key(key))}</th><td>{v}</td></tr>")
    return "".join(rows)


def _closest_setup_html(setup: dict | None) -> str:
    """The single closest (smallest dollar-distance-to-trigger) setup
    candidate, same visual weight as the resistance/support tables below
    it -- the headline read, not a footnote."""
    if setup is None:
        return "<h3>Closest setup</h3><p class='muted'>none currently watchable</p>"
    label = _SETUP_TYPE_LABELS.get(setup["setup_type"], setup["setup_type"])
    return (
        f"<h3>Closest setup: {html.escape(label)} @ {_fmt(setup['trigger_price'], 2)} "
        f"(${_fmt(setup['distance'], 2)} away)</h3>"
        "<table class='detail'>"
        f"{_setup_hold_and_factor_rows_html(setup)}"
        "</table>"
    )


def _setup_chips_html(symbol: str, others: list[dict]) -> str:
    """The remaining (non-closest) setup candidates as compact chips --
    type name + dollar distance only -- each immediately followed by its
    own initially-hidden detail table, toggled by a click handler
    delegated on #symbols (see _SCRIPT). Hidden markup ships in the same
    render rather than being fetched on click, since all the data is
    already in `state` -- no round trip needed to expand one.

    `data-key` uniquely identifies this chip across a full #symbols
    innerHTML rebuild (symbol + setup type) so refresh()'s JS can restore
    "was this expanded before this poll" -- fixed 2026-09-17: without it,
    an expanded section silently re-collapsed on the very next ~4s poll,
    since a freshly-rebuilt chip always starts hidden and nothing
    remembered which ones the user had open."""
    if not others:
        return ""
    parts = ["<div class='setup-chips'>"]
    for setup in others:
        label = _SETUP_TYPE_LABELS.get(setup["setup_type"], setup["setup_type"])
        key = html.escape(f"{symbol}:setup:{setup['setup_type']}")
        parts.append(
            f"<button type='button' class='setup-chip' data-key='{key}'>"
            f"{html.escape(label)} &middot; ${_fmt(setup['distance'], 2)}</button>"
            "<div class='setup-detail' hidden><table class='detail'>"
            f"{_setup_hold_and_factor_rows_html(setup)}"
            "</table></div>"
        )
    parts.append("</div>")
    return "".join(parts)


def _level_rows_html(block: dict) -> str:
    c, h = block["components"], block["hold"]
    badge = (
        "<span class='badge badge-confirmed'>confirmed</span>" if h["confirmed"]
        else "<span class='badge badge-pending'>not confirmed</span>"
    )
    return (
        f"<tr><th>strength</th><td>{_fmt(block['strength_score'], 2)}</td></tr>"
        f"<tr><th>touch count</th><td>{c['touch_count']}</td></tr>"
        f"<tr><th>touch volume</th><td>{_fmt(c['total_touch_volume'], 0)}</td></tr>"
        f"<tr><th>round-number bonus</th><td>{_fmt(c['round_number_bonus'], 2)}</td></tr>"
        f"<tr><th>hold direction</th><td>{html.escape(h['direction'])}</td></tr>"
        f"<tr><th>consecutive closes</th><td>{h['consecutive_bars']} / {h['required_bars']}</td></tr>"
        f"<tr><th>hold confirmed</th><td>{badge}</td></tr>"
        f"<tr><th>failed attempts</th><td>{h['failed_attempts']}</td></tr>"
    )


def _level_block_html(symbol: str, kind: str, title: str, block: dict | None) -> str:
    """Collapsed by default behind the same click-to-expand chip pattern
    as the other three setup-type candidates (phase 3.5's setup-chip /
    setup-detail, reusing the exact same generic click handler -- no new
    JS wiring needed). The closest-setup callout already shows resistance
    breakout in full when that's the closest type, so an always-open raw
    table here duplicated the same information by default; now one click
    away instead of always taking the vertical space.

    `data-key` (symbol + "level" + kind) lets refresh()'s JS restore
    expanded state across a poll rebuild -- see _setup_chips_html."""
    if block is None:
        return (
            "<button type='button' class='setup-chip' disabled>"
            f"{html.escape(title)}: none on this side of price</button>"
        )
    key = html.escape(f"{symbol}:level:{kind}")
    return (
        f"<button type='button' class='setup-chip' data-key='{key}'>{html.escape(title)} "
        f"@ {_fmt(block['price'], 2)}</button>"
        "<div class='setup-detail' hidden><table class='detail'>"
        f"{_level_rows_html(block)}"
        "</table></div>"
    )


def _level_chips_html(symbol: str, resistance: dict | None, support: dict | None) -> str:
    return (
        "<div class='setup-chips'>"
        f"{_level_block_html(symbol, 'resistance', 'Resistance (nearest above)', resistance)}"
        f"{_level_block_html(symbol, 'support', 'Support (nearest below)', support)}"
        "</div>"
    )


_EXIT_PHASE_LABELS = {
    "swing_low": "swing-low anchored (early)",
    "trailing": "flat trailing",
}


def _journal_open_html(open_block: dict | None) -> str:
    if open_block is None:
        return "<p class='muted'>No open virtual position.</p>"
    cls = _sign_class(open_block["unrealized_pnl_pct"])
    shares = open_block.get("shares")
    # A genuinely computed zero-share entry (specs.md section 7: "the
    # trade still logs ... but visibly flagged as zero-size") must never
    # look like a real position at a glance -- flagged distinctly from
    # both a real share count and a pre-migration "never computed" dash.
    zero_flag = (" <span class='zero-size-flag'>zero-size — no real position</span>"
                if shares == 0 else "")
    phase = open_block.get("exit_phase")
    phase_label = _EXIT_PHASE_LABELS.get(phase, phase)
    return (
        "<table class='detail'>"
        f"<tr><th>symbol</th><td>{html.escape(str(open_block['symbol']))}</td></tr>"
        f"<tr><th>entry price</th><td>{_fmt(open_block['entry_price'], 2)}</td></tr>"
        f"<tr><th>trailing stop</th><td>{_fmt(open_block['stop_level'], 2)}</td></tr>"
        f"<tr><th>stop phase</th><td>{html.escape(str(phase_label))}</td></tr>"
        f"<tr><th>shares</th><td>{_fmt(shares)}{zero_flag}</td></tr>"
        f"<tr><th>unrealized P&amp;L %</th>"
        f"<td class='{cls}'>{_fmt(open_block['unrealized_pnl_pct'], 2)}%</td></tr>"
        f"<tr><th>unrealized P&amp;L $</th>"
        f"<td class='{cls}'>{_fmt_dollars(open_block.get('unrealized_pnl_dollars'))}</td></tr>"
        "</table>"
    )


def _journal_closed_rows_html(closed: list[dict]) -> str:
    if not closed:
        return "<tr><td colspan='10' class='muted'>No closed trades yet.</td></tr>"
    rows = []
    for t in closed:
        # symbol_switched isn't a trading outcome -- it's watchlist
        # housekeeping (the symbol got unwatched/evicted while a position
        # was open, so it was force-closed at whatever price happened to
        # be current). Muted end to end, including overriding the pos/neg
        # P&L coloring other rows get, so nobody reads it as a real
        # win/loss at a glance -- see specs.md section 6.
        is_housekeeping = t["exit_reason"] == "symbol_switched"
        cls = "muted" if is_housekeeping else _sign_class(t["realized_pnl_pct"])
        pnl_pct = "" if t["realized_pnl_pct"] is None else f"{_fmt(t['realized_pnl_pct'], 2)}%"
        pnl_dollars = _fmt_dollars(t.get("realized_pnl_dollars"))
        shares = t.get("shares")
        # Same zero-size-vs-never-computed distinction as the open
        # position block (specs.md section 7) -- 0 flagged, None a dash.
        shares_html = (f"{shares} <span class='zero-size-flag'>zero-size</span>"
                      if shares == 0 else _fmt(shares))
        row_open = "<tr class='row-housekeeping'>" if is_housekeeping else "<tr>"
        rows.append(
            f"{row_open}"
            f"<td>{html.escape(str(t['symbol']))}</td>"
            f"<td>{_fmt_ts(t.get('entry_ts'))}</td>"
            f"<td>{_fmt(t['entry_price'], 2)}</td>"
            f"<td>{_fmt_ts(t.get('exit_ts'))}</td>"
            f"<td>{_fmt(t['exit_price'], 2)}</td>"
            f"<td>{html.escape(str(t['exit_reason']))}</td>"
            f"<td>{shares_html}</td>"
            f"<td class='{cls}'>{pnl_pct}</td>"
            f"<td class='{cls}'>{pnl_dollars}</td>"
            "<td><button type='button' class='remove-btn journal-delete-btn' "
            f"data-trade-id='{t['id']}'>delete</button></td>"
            "</tr>"
        )
    return "".join(rows)


def _remove_button_html(symbol: str) -> str:
    sym = html.escape(symbol)
    return f"<button type='button' class='remove-btn' data-symbol='{sym}'>remove</button>"


def _watch_note_html(note: str | None) -> str:
    # Prominent, near the symbol/price header -- specs.md section 7's
    # highest-priority gap: "no trade record currently captures why a
    # symbol was worth watching." Plainly shown when empty, not just
    # omitted, so a missing note is never confused with "hasn't loaded
    # yet". No special update wiring needed beyond this -- it's part of
    # symbolCardHtml's own output, so it refreshes the same way the rest
    # of the card does on every push (the whole card is replaced, not
    # patched piecemeal).
    text = html.escape(note) if note else "no note recorded"
    cls = "watch-note muted" if not note else "watch-note"
    return f"<p class='{cls}'>{text}</p>"


def _reverse_splits_html(splits: list[dict] | None) -> str:
    # Quiet unless there's something to flag -- unlike watch_note (which
    # always renders, since "no note" needs to read differently from
    # "hasn't loaded yet"), reverse_splits_for always resolves instantly
    # and definitively (specs.md section 7), so an empty result has no
    # such ambiguity to guard against; showing "no known reverse splits"
    # on every panel (the common case for most tickers) would be pure
    # noise for what's meant to read as a warning banner.
    if not splits:
        return ""
    items = "; ".join(
        html.escape(s["ratio"]) + " on " + html.escape(s["split_date"])
        + (f" ({html.escape(s['note'])})" if s.get("note") else "")
        for s in splits
    )
    return f"<p class='reverse-split-flag'>⚠ Reverse-split history: {items}</p>"


def _continuation_html(status: dict | None) -> str:
    # ALWAYS rendered (specs.md section 7) -- deliberately NOT the
    # reverse-split flag's "only when non-empty" treatment: "Day 1,
    # fresh" is just as useful to see at a glance as a real flag, and
    # "unknown" (daily history never fetched, or the fetch failed) must
    # never be silently indistinguishable from either real answer.
    status = status or {"status": "unknown"}
    kind = status.get("status")
    if kind == "fresh":
        lookback = status.get("lookback_days_used")
        threshold_pct = status.get("threshold_pct_used") or 0.0
        text = (f"Day 1 (fresh) — no moves over {threshold_pct * 100:.0f}% "
                f"in the past {lookback} trading days")
        cls = "continuation-flag"
    elif kind == "continuation":
        items = "; ".join(
            f"{d['pct_change'] * 100:+.1f}% on {_fmt_date(d['ts'])}"
            for d in status.get("days", [])
        )
        text = f"Continuation — {items}"
        cls = "continuation-flag continuation-flag-active"
    else:
        text = "Continuation status: unknown (no daily history available)"
        cls = "continuation-flag muted"
    return f"<p class='{cls}'>{html.escape(text)}</p>"


def _symbol_card_html(symbol: str, state: dict) -> str:
    """One grid panel per watched symbol: the same per-block renderers
    phase 1's single-symbol page used, plus a remove control scoped to
    this panel's own symbol (data-symbol, wired via event delegation on
    #symbols in _SCRIPT -- see refresh())."""
    sym = html.escape(symbol)
    note_html = _watch_note_html(state.get("watch_note"))
    split_html = _reverse_splits_html(state.get("reverse_splits"))
    continuation_html = _continuation_html(state.get("continuation"))
    if state.get("status") != "ok":
        msg = (f"Warming up — waiting for bars for {sym}." if state.get("symbol")
              else "No data yet.")
        return (f"<section class='card' data-symbol='{sym}'>"
                f"<div class='hero'><h2>{sym}</h2>{_remove_button_html(symbol)}</div>"
                f"{note_html}"
                f"{split_html}"
                f"{continuation_html}"
                f"<p class='muted'>{html.escape(msg)}</p></section>")

    s = state["session"]
    price_cls = _cmp_class(state["last_price"], s["vwap"])
    ema9_cls = _cmp_class(state["last_price"], s["ema9"])
    hist_cls = _sign_class(s["macd"]["histogram"])
    setups = state.get("setups") or []
    closest, others = (setups[0], setups[1:]) if setups else (None, [])
    return f"""
<section class="card" data-symbol="{sym}">
  <div class="hero">
    <div class="hero-symbol">{sym}</div>
    <div class="hero-price {price_cls}">{_fmt(state['last_price'], 2)}</div>
    {_remove_button_html(symbol)}
  </div>
  {note_html}
  {split_html}
  {continuation_html}
  <table class="detail">
    <tr><th>Bars</th><td>{state['bar_count']}</td></tr>
    <tr><th>Last bar extended-hours</th><td>{"yes" if state["last_bar_is_extended"] else "no"}</td></tr>
    <tr><th>VWAP (session)</th><td>{_fmt(s['vwap'])}</td></tr>
    <tr><th>EMA 9</th><td class="{ema9_cls}">{_fmt(s['ema9'])}</td></tr>
    <tr><th>EMA 20</th><td>{_fmt(s['ema20'])}</td></tr>
    <tr><th>MACD</th><td>{_fmt(s['macd']['macd'], 6)}</td></tr>
    <tr><th>MACD signal</th><td>{_fmt(s['macd']['signal'], 6)}</td></tr>
    <tr><th>MACD histogram</th><td class="{hist_cls}">{_fmt(s['macd']['histogram'], 6)}</td></tr>
    <tr><th>Relative volume</th><td>{_fmt(s['relative_volume'], 2)}</td></tr>
  </table>
  {_closest_setup_html(closest)}
  {_setup_chips_html(symbol, others)}
  <h3>Virtual position</h3>
  {_journal_open_html(state["journal"]["open"])}
  {_level_chips_html(symbol, state["levels"]["resistance"], state["levels"]["support"])}
</section>
"""


def _watch_form_html(count: int, max_symbols: int) -> str:
    # Recording why a symbol is worth watching is part of the SAME
    # action as adding it, not a second step (specs.md section 7) --
    # the note field sits right next to the symbol input, submitted in
    # the same POST /api/watch call. maxlength here is a UX nicety, not
    # the real validation -- that's the server's MAX_WATCH_NOTE_LENGTH
    # check (a client-side-only limit could silently disagree with it).
    return f"""
<form id="watch-form" class="watch-form">
  <input type="text" id="watch-input" placeholder="Add symbol (e.g. NVDA)" maxlength="10" autocomplete="off">
  <input type="text" id="watch-note-input" placeholder="Why watching? (optional)" maxlength="500" autocomplete="off">
  <button type="submit">Add</button>
  <span id="slot-count" class="muted">{count} / {max_symbols} symbols watched</span>
  <span id="watch-status" class="muted"></span>
</form>
"""


def _strategy_params_html(params: dict) -> str:
    # Read-only display (specs.md section 8: "no UI element required this
    # pass beyond maybe displaying current values read-only somewhere
    # convenient" -- the adjustment mechanism itself is API-only for now,
    # a settings UI is a separate follow-up). id="strategy-params" so
    # render() can refresh it in place on every push, same pattern as
    # every other element this page updates without a full reload.
    parts = []
    for key in sorted(params):
        p = params[key]
        when = _fmt_ts(p.get("updated_at")) if p.get("updated_at") else "seed default"
        parts.append(f"{html.escape(key)}={_fmt(p['value'], 4)} (since {when})")
    return " · ".join(parts)


def _current_equity_html(value: float) -> str:
    # Prominent, above the strategy-params line (specs.md section 7) --
    # id="current-equity" so render() can refresh it in place on every
    # push, same pattern as strategy-params itself.
    return f"<p id='current-equity'><strong>Current equity: {_fmt_dollars(value)}</strong></p>"


def _page(full_states: dict[str, dict], recent_closed: list[dict], poll_enabled: bool,
          max_symbols: int, strategy_params: dict, current_equity: float) -> str:
    if full_states:
        cards_html = "".join(_symbol_card_html(sym, st) for sym, st in full_states.items())
    else:
        cards_html = ("<p class='muted'>No symbols watched yet — add one below "
                      f"(up to {max_symbols}).</p>")

    body = f"""
{_current_equity_html(current_equity)}
<p id="strategy-params" class="muted">{_strategy_params_html(strategy_params)}</p>
{_watch_form_html(len(full_states), max_symbols)}
<div id="symbols" class="grid">{cards_html}</div>
<section class="card">
  <div class="hero">
    <h2>Recent closed trades</h2>
    <button type="button" id="clear-symbol-switched-btn" class="remove-btn">clear symbol_switched rows</button>
    <span id="clear-status" class="muted"></span>
  </div>
  <table class="detail">
    <tr><th>symbol</th><th>entry time</th><th>entry</th><th>exit time</th>
        <th>exit</th><th>reason</th><th>shares</th><th>P&amp;L %</th>
        <th>P&amp;L $</th><th></th></tr>
    <tbody id="journal-closed-tbody">{_journal_closed_rows_html(recent_closed)}</tbody>
  </table>
</section>
"""
    return _wrap(body, poll_enabled)


# Live-polling toggle -- one global switch for the whole poller (all
# watched symbols), not per-symbol. poll_enabled reflects server-side
# truth (Poller.poll_enabled) at request time, same "real data on first
# paint" approach as the rest of this page.
def _poll_toggle_html(poll_enabled: bool) -> str:
    label = "Pause updates" if poll_enabled else "Resume updates"
    status = "live" if poll_enabled else "paused"
    return (
        "<div class='poll-controls'>"
        f"<span id=\"poll-status\" class='muted'>{status}</span>"
        f"<button type='button' id=\"poll-toggle\">{label}</button>"
        "</div>"
    )

# Mirrors, in JS, the same helpers/templates as the Python side above --
# see the module-level comment on _fmt for why this duplication exists.
# Stage A interim: rebuilds the whole #symbols container's innerHTML each
# poll (not per-element targeted updates the way phase 1's single-symbol
# page did) -- a coarser-grained update, still no meta-refresh/page
# reload, appropriate for a set of symbols that can change size between
# polls. Stage B's real multi-panel grid replaces this.
_SCRIPT = """
function fmt(v, d) {
  if (v === null || v === undefined) return '\\u2014';
  if (typeof v === 'number') return v.toFixed(d === undefined ? 4 : d);
  return String(v);
}
// Mirrors _fmt_dollars (Python side).
function fmtDollars(v) {
  if (v === null || v === undefined) return '\\u2014';
  const sign = v < 0 ? '-' : '';
  return sign + '$' + Math.abs(v).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
}
// Human-readable, America/New_York -- same timezone convention as the
// Python-rendered first paint (app.py's _fmt_ts), forced explicitly via
// the Intl timeZone option regardless of the browser's own local zone
// (Part D, 2026-09-17: entry_ts/exit_ts existed since phase 4, never shown).
function fmtTs(ts) {
  if (ts === null || ts === undefined) return '\\u2014';
  return new Date(ts * 1000).toLocaleString('en-US', {
    timeZone: 'America/New_York', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  });
}
// Mirrors _fmt_date (Python side) -- date only, for a DAILY bar's ts.
function fmtDate(ts) {
  if (ts === null || ts === undefined) return '\\u2014';
  return new Date(ts * 1000).toLocaleString('en-US', {
    timeZone: 'America/New_York', month: '2-digit', day: '2-digit',
  });
}
function signClass(v) {
  if (v === null || v === undefined) return '';
  return v >= 0 ? 'pos' : 'neg';
}
function cmpClass(a, b) {
  if (a === null || a === undefined || b === null || b === undefined) return '';
  return a >= b ? 'pos' : 'neg';
}
function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}
function levelRows(block) {
  const h = block.hold, c = block.components;
  const badge = h.confirmed
    ? '<span class="badge badge-confirmed">confirmed</span>'
    : '<span class="badge badge-pending">not confirmed</span>';
  return '<tr><th>strength</th><td>' + fmt(block.strength_score, 2) + '</td></tr>' +
    '<tr><th>touch count</th><td>' + c.touch_count + '</td></tr>' +
    '<tr><th>touch volume</th><td>' + fmt(c.total_touch_volume, 0) + '</td></tr>' +
    '<tr><th>round-number bonus</th><td>' + fmt(c.round_number_bonus, 2) + '</td></tr>' +
    '<tr><th>hold direction</th><td>' + esc(h.direction) + '</td></tr>' +
    '<tr><th>consecutive closes</th><td>' + h.consecutive_bars + ' / ' + h.required_bars + '</td></tr>' +
    '<tr><th>hold confirmed</th><td>' + badge + '</td></tr>' +
    '<tr><th>failed attempts</th><td>' + h.failed_attempts + '</td></tr>';
}
// Collapsed by default behind the same setup-chip/setup-detail toggle
// pattern as the other three setup-type candidates -- see the Python
// side's _level_block_html for why. data-key (symbol + level kind) lets
// refresh() restore expanded state across a poll rebuild.
function levelBlockHtml(symbol, kind, title, block) {
  if (!block) {
    return '<button type="button" class="setup-chip" disabled>' + esc(title) +
      ': none on this side of price</button>';
  }
  const key = esc(symbol + ':level:' + kind);
  return '<button type="button" class="setup-chip" data-key="' + key + '">' + esc(title) + ' @ ' +
    fmt(block.price, 2) + '</button>' +
    '<div class="setup-detail" hidden><table class="detail">' + levelRows(block) + '</table></div>';
}
function levelChipsHtml(symbol, resistance, support) {
  return '<div class="setup-chips">' +
    levelBlockHtml(symbol, 'resistance', 'Resistance (nearest above)', resistance) +
    levelBlockHtml(symbol, 'support', 'Support (nearest below)', support) +
    '</div>';
}
var EXIT_PHASE_LABELS = {
  swing_low: 'swing-low anchored (early)',
  trailing: 'flat trailing',
};
function journalOpenHtml(open) {
  if (!open) return '<p class="muted">No open virtual position.</p>';
  const cls = signClass(open.unrealized_pnl_pct);
  const zeroFlag = open.shares === 0
    ? ' <span class="zero-size-flag">zero-size \\u2014 no real position</span>' : '';
  const phaseLabel = EXIT_PHASE_LABELS[open.exit_phase] || open.exit_phase;
  return '<table class="detail">' +
    '<tr><th>symbol</th><td>' + esc(open.symbol) + '</td></tr>' +
    '<tr><th>entry price</th><td>' + fmt(open.entry_price, 2) + '</td></tr>' +
    '<tr><th>trailing stop</th><td>' + fmt(open.stop_level, 2) + '</td></tr>' +
    '<tr><th>stop phase</th><td>' + esc(phaseLabel) + '</td></tr>' +
    '<tr><th>shares</th><td>' + fmt(open.shares, 0) + zeroFlag + '</td></tr>' +
    '<tr><th>unrealized P&amp;L %</th><td class="' + cls + '">' + fmt(open.unrealized_pnl_pct, 2) + '%</td></tr>' +
    '<tr><th>unrealized P&amp;L $</th><td class="' + cls + '">' + fmtDollars(open.unrealized_pnl_dollars) + '</td></tr>' +
    '</table>';
}
function journalClosedRows(closed) {
  if (!closed || !closed.length) {
    return '<tr><td colspan="10" class="muted">No closed trades yet.</td></tr>';
  }
  return closed.map(function(t) {
    // symbol_switched = watchlist housekeeping, not a trading outcome --
    // see _journal_closed_rows_html's comment (Python side) for why the
    // whole row is muted, overriding pos/neg P&L coloring too.
    const isHousekeeping = t.exit_reason === 'symbol_switched';
    const cls = isHousekeeping ? 'muted' : signClass(t.realized_pnl_pct);
    const pnlPct = t.realized_pnl_pct === null ? '' : fmt(t.realized_pnl_pct, 2) + '%';
    const pnlDollars = fmtDollars(t.realized_pnl_dollars);
    const sharesHtml = t.shares === 0
      ? '0 <span class="zero-size-flag">zero-size</span>' : fmt(t.shares, 0);
    const rowOpen = isHousekeeping ? '<tr class="row-housekeeping">' : '<tr>';
    return rowOpen + '<td>' + esc(t.symbol) + '</td>' +
      '<td>' + fmtTs(t.entry_ts) + '</td><td>' + fmt(t.entry_price, 2) + '</td>' +
      '<td>' + fmtTs(t.exit_ts) + '</td><td>' + fmt(t.exit_price, 2) + '</td>' +
      '<td>' + esc(t.exit_reason) + '</td>' +
      '<td>' + sharesHtml + '</td>' +
      '<td class="' + cls + '">' + pnlPct + '</td>' +
      '<td class="' + cls + '">' + pnlDollars + '</td>' +
      '<td><button type="button" class="remove-btn journal-delete-btn" data-trade-id="' +
      esc(t.id) + '">delete</button></td></tr>';
  }).join('');
}
function removeButtonHtml(symbol) {
  return '<button type="button" class="remove-btn" data-symbol="' + esc(symbol) + '">remove</button>';
}
var SETUP_TYPE_LABELS = {
  resistance_breakout: 'Resistance breakout',
  micro_breakout: 'Micro-breakout',
  vwap_reclaim: 'VWAP pullback-reclaim',
  round_number_reclaim: 'Round-number reclaim',
};
function humanizeKey(k) {
  return k.split('_').join(' ');
}
function setupHoldAndFactorRows(setup) {
  const h = setup.hold;
  const badge = h.confirmed
    ? '<span class="badge badge-confirmed">confirmed</span>'
    : '<span class="badge badge-pending">not confirmed</span>';
  let rows = '<tr><th>hold direction</th><td>' + esc(h.direction) + '</td></tr>' +
    '<tr><th>consecutive closes</th><td>' + h.consecutive_bars + ' / ' + h.required_bars + '</td></tr>' +
    '<tr><th>hold confirmed</th><td>' + badge + '</td></tr>' +
    '<tr><th>failed attempts</th><td>' + h.failed_attempts + '</td></tr>';
  Object.keys(setup.factors).forEach(function (key) {
    const raw = setup.factors[key];
    const v = typeof raw === 'boolean' ? (raw ? 'yes' : 'no')
      : typeof raw === 'number' ? fmt(raw, 4) : esc(raw);
    rows += '<tr><th>' + esc(humanizeKey(key)) + '</th><td>' + v + '</td></tr>';
  });
  return rows;
}
function closestSetupHtml(setup) {
  if (!setup) return '<h3>Closest setup</h3><p class="muted">none currently watchable</p>';
  const label = SETUP_TYPE_LABELS[setup.setup_type] || setup.setup_type;
  return '<h3>Closest setup: ' + esc(label) + ' @ ' + fmt(setup.trigger_price, 2) +
    ' ($' + fmt(setup.distance, 2) + ' away)</h3>' +
    '<table class="detail">' + setupHoldAndFactorRows(setup) + '</table>';
}
function setupChipsHtml(symbol, others) {
  if (!others || !others.length) return '';
  let out = '<div class="setup-chips">';
  others.forEach(function (setup) {
    const label = SETUP_TYPE_LABELS[setup.setup_type] || setup.setup_type;
    const key = esc(symbol + ':setup:' + setup.setup_type);
    out += '<button type="button" class="setup-chip" data-key="' + key + '">' + esc(label) + ' &middot; $' +
      fmt(setup.distance, 2) + '</button>' +
      '<div class="setup-detail" hidden><table class="detail">' +
      setupHoldAndFactorRows(setup) + '</table></div>';
  });
  out += '</div>';
  return out;
}
function watchNoteHtml(note) {
  // Mirrors _watch_note_html (Python side) -- plainly shown when empty,
  // not omitted, so it's never confused with "hasn't loaded yet."
  const text = note ? esc(note) : 'no note recorded';
  const cls = note ? 'watch-note' : 'watch-note muted';
  return '<p class="' + cls + '">' + text + '</p>';
}
function reverseSplitsHtml(splits) {
  // Mirrors _reverse_splits_html (Python side) -- quiet unless there's
  // something to flag, see that function's comment for why.
  if (!splits || !splits.length) return '';
  const items = splits.map(function (s) {
    const note = s.note ? ' (' + esc(s.note) + ')' : '';
    return esc(s.ratio) + ' on ' + esc(s.split_date) + note;
  }).join('; ');
  return '<p class="reverse-split-flag">\\u26a0 Reverse-split history: ' + items + '</p>';
}
function continuationHtml(status) {
  // Mirrors _continuation_html (Python side) -- ALWAYS rendered, unlike
  // reverseSplitsHtml's "only when non-empty" treatment; see that
  // function's own comment for why "unknown" must stay distinct from
  // both real answers.
  status = status || {status: 'unknown'};
  var text, cls;
  if (status.status === 'fresh') {
    var thresholdPct = (status.threshold_pct_used || 0) * 100;
    text = 'Day 1 (fresh) \\u2014 no moves over ' + thresholdPct.toFixed(0) +
      '% in the past ' + status.lookback_days_used + ' trading days';
    cls = 'continuation-flag';
  } else if (status.status === 'continuation') {
    var items = (status.days || []).map(function (d) {
      var pct = d.pct_change * 100;
      return (pct >= 0 ? '+' : '') + pct.toFixed(1) + '% on ' + fmtDate(d.ts);
    }).join('; ');
    text = 'Continuation \\u2014 ' + items;
    cls = 'continuation-flag continuation-flag-active';
  } else {
    text = 'Continuation status: unknown (no daily history available)';
    cls = 'continuation-flag muted';
  }
  return '<p class="' + cls + '">' + esc(text) + '</p>';
}
function symbolCardHtml(symbol, state) {
  const sym = esc(symbol);
  const noteHtml = watchNoteHtml(state.watch_note);
  const splitHtml = reverseSplitsHtml(state.reverse_splits);
  const continuationHtmlStr = continuationHtml(state.continuation);
  if (state.status !== 'ok') {
    return '<section class="card" data-symbol="' + sym + '">' +
      '<div class="hero"><h2>' + sym + '</h2>' + removeButtonHtml(symbol) + '</div>' +
      noteHtml + splitHtml + continuationHtmlStr +
      '<p class="muted">Warming up \\u2014 waiting for bars.</p></section>';
  }
  const s = state.session;
  const priceCls = cmpClass(state.last_price, s.vwap);
  const ema9Cls = cmpClass(state.last_price, s.ema9);
  const histCls = signClass(s.macd.histogram);
  const setups = state.setups || [];
  const closest = setups.length ? setups[0] : null;
  const others = setups.length ? setups.slice(1) : [];
  return '<section class="card" data-symbol="' + sym + '">' +
    '<div class="hero"><div class="hero-symbol">' + sym + '</div>' +
    '<div class="hero-price ' + priceCls + '">' + fmt(state.last_price, 2) + '</div>' +
    removeButtonHtml(symbol) + '</div>' +
    noteHtml + splitHtml + continuationHtmlStr +
    '<table class="detail">' +
    '<tr><th>Bars</th><td>' + state.bar_count + '</td></tr>' +
    '<tr><th>Last bar extended-hours</th><td>' + (state.last_bar_is_extended ? 'yes' : 'no') + '</td></tr>' +
    '<tr><th>VWAP (session)</th><td>' + fmt(s.vwap, 4) + '</td></tr>' +
    '<tr><th>EMA 9</th><td class="' + ema9Cls + '">' + fmt(s.ema9, 4) + '</td></tr>' +
    '<tr><th>EMA 20</th><td>' + fmt(s.ema20, 4) + '</td></tr>' +
    '<tr><th>MACD</th><td>' + fmt(s.macd.macd, 6) + '</td></tr>' +
    '<tr><th>MACD signal</th><td>' + fmt(s.macd.signal, 6) + '</td></tr>' +
    '<tr><th>MACD histogram</th><td class="' + histCls + '">' + fmt(s.macd.histogram, 6) + '</td></tr>' +
    '<tr><th>Relative volume</th><td>' + fmt(s.relative_volume, 2) + '</td></tr>' +
    '</table>' +
    closestSetupHtml(closest) +
    setupChipsHtml(symbol, others) +
    '<h3>Virtual position</h3>' + journalOpenHtml(state.journal.open) +
    levelChipsHtml(symbol, state.levels.resistance, state.levels.support) +
    '</section>';
}
// render(data) does the actual DOM patch, driven by EITHER a push
// (EventSource.onmessage) or the explicit post-action fetch() below --
// same DOM-patch approach either way (fixed 2026-09-17: no full-page or
// full-#symbols replace beyond what's needed), so a push arriving
// mid-interaction is no more disruptive than a poll-triggered one was.
function render(data) {
  applyPollUiState(data.poll_enabled);  // stay in sync even if another tab paused/resumed it
  const symbolsEl = document.getElementById('symbols');

  // Preserve which setup-chip/level-chip detail sections are currently
  // expanded across this render's full innerHTML rebuild -- fixed
  // 2026-09-17: without this, an expanded section silently re-collapsed
  // on the very next update, since #symbols' whole subtree gets rebuilt
  // from scratch every render() and a freshly-built chip always starts
  // hidden, with nothing remembering which ones were open. Still applies
  // now that updates are pushed rather than polled -- a push can land
  // mid-interaction just as easily as a poll could.
  const expandedKeys = new Set();
  symbolsEl.querySelectorAll('.setup-chip[data-key]').forEach(function (chip) {
    const detail = chip.nextElementSibling;
    if (detail && !detail.hidden) expandedKeys.add(chip.getAttribute('data-key'));
  });

  const syms = Object.keys(data.symbols || {});
  const maxSymbols = data.max_symbols || 4;
  symbolsEl.innerHTML = syms.length
    ? syms.map(function(sym) { return symbolCardHtml(sym, data.symbols[sym]); }).join('')
    : '<p class="muted">No symbols watched yet \\u2014 add one below (up to ' + maxSymbols + ').</p>';

  symbolsEl.querySelectorAll('.setup-chip[data-key]').forEach(function (chip) {
    if (!expandedKeys.has(chip.getAttribute('data-key'))) return;
    const detail = chip.nextElementSibling;
    if (detail) detail.hidden = false;
  });

  document.getElementById('journal-closed-tbody').innerHTML = journalClosedRows(data.recent_closed);
  document.getElementById('slot-count').textContent = syms.length + ' / ' + maxSymbols + ' symbols watched';
  const paramsEl = document.getElementById('strategy-params');
  if (paramsEl && data.strategy_params) paramsEl.textContent = strategyParamsText(data.strategy_params);
  const equityEl = document.getElementById('current-equity');
  if (equityEl && data.current_equity !== undefined) {
    equityEl.innerHTML = '<strong>Current equity: ' + fmtDollars(data.current_equity) + '</strong>';
  }
}

// Mirrors _strategy_params_html (Python side) -- read-only display, kept
// in sync on every push the same way as everything else on this page.
function strategyParamsText(params) {
  return Object.keys(params).sort().map(function (key) {
    const p = params[key];
    const when = p.updated_at ? fmtTs(p.updated_at) : 'seed default';
    return key + '=' + fmt(p.value, 4) + ' (since ' + when + ')';
  }).join(' \\u00b7 ');
}

// Thin wrapper kept for the explicit post-action call sites below
// (watch/unwatch/journal delete/clear/poll-toggle) -- a same-request-
// cycle fetch is snappier for the tab that just took the action than
// waiting on the next push, which the SSE connection below still
// delivers to every OTHER open tab.
async function refresh() {
  let data;
  try {
    const r = await fetch('/api/state');
    data = await r.json();
  } catch (e) {
    return;  // keep showing the last-good render, same philosophy as the server's own poll_ok
  }
  render(data);
}

// Add-symbol form: POSTs /api/watch, which ADDS to the watched set (fills
// the next empty slot) rather than replacing whatever's already watched --
// a rejected add (a duplicate, or an invalid ticker) shows the server's
// own reason text right in the form, not a silent failure. Adding while
// already at max_symbols is NOT a rejection -- the server evicts the
// oldest-added symbol to make room and reports that in "reason" even
// though "ok" is true, so that path is shown too (muted, not the "neg"
// error styling a real rejection gets).
document.getElementById('watch-form').addEventListener('submit', async function (e) {
  e.preventDefault();
  const input = document.getElementById('watch-input');
  const noteInput = document.getElementById('watch-note-input');
  const statusEl = document.getElementById('watch-status');
  const symbol = input.value.trim();
  if (!symbol) return;
  statusEl.textContent = '';
  statusEl.className = 'muted';
  let body;
  try {
    // note is optional -- recording why is part of the SAME add action
    // (specs.md section 7), submitted in this one request, never a
    // second step or a reason to slow down/block adding the symbol.
    const r = await fetch('/api/watch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: 'symbol=' + encodeURIComponent(symbol) +
        '&note=' + encodeURIComponent(noteInput.value.trim()),
    });
    body = await r.json();
  } catch (err) {
    statusEl.textContent = 'request failed';
    statusEl.className = 'neg';
    return;
  }
  if (body.ok) {
    input.value = '';
    noteInput.value = '';
    statusEl.textContent = body.reason || '';
    statusEl.className = 'muted';
  } else {
    statusEl.textContent = body.reason;
    statusEl.className = 'neg';
  }
  refresh();
});

// Setup chip expand/collapse: purely local DOM toggle, no fetch, no
// refresh() -- delegated on #symbols for the same reason as the remove
// control below. Expanded state IS preserved across the next render(),
// whether it's push- or fetch()-triggered -- see render()'s expandedKeys
// save/restore above.
document.getElementById('symbols').addEventListener('click', function (e) {
  const chip = e.target.closest('.setup-chip');
  if (!chip) return;
  const detail = chip.nextElementSibling;
  if (detail) detail.hidden = !detail.hidden;
});

// Remove control: one per panel, delegated on the #symbols container so it
// keeps working after refresh() replaces the container's innerHTML (no
// re-binding needed on every poll).
document.getElementById('symbols').addEventListener('click', async function (e) {
  const btn = e.target.closest('.remove-btn');
  if (!btn) return;
  const symbol = btn.getAttribute('data-symbol');
  btn.disabled = true;
  try {
    await fetch('/api/unwatch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: 'symbol=' + encodeURIComponent(symbol),
    });
  } catch (err) {
    // leave the panel as-is; the next poll will reflect actual state either way
  }
  refresh();
});

// Per-row journal delete: delegated on #journal-closed-tbody (the tbody
// element itself persists across refresh()'s innerHTML rebuild, same
// reasoning as #symbols above). This is a PERMANENT SQLite delete --
// confirm() first, same "never silently delete" standard the rest of
// this project holds to (specs.md).
document.getElementById('journal-closed-tbody').addEventListener('click', async function (e) {
  const btn = e.target.closest('.journal-delete-btn');
  if (!btn) return;
  if (!confirm('Permanently delete this closed trade row?')) return;
  const tradeId = btn.getAttribute('data-trade-id');
  btn.disabled = true;
  try {
    await fetch('/api/journal/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: 'id=' + encodeURIComponent(tradeId),
    });
  } catch (err) {
    // leave the row as-is; the next poll reflects actual state either way
  }
  refresh();
});

// Bulk clear: NOT delegated -- this button lives outside #symbols/
// #journal-closed-tbody, in the part of the page refresh() never
// replaces, so a direct one-time binding is enough (same as the poll
// toggle button below). Also a permanent SQLite delete -- confirm() first.
//
// The status span exists because a 0-row delete is a real, correct
// success (nothing to clear) -- found live 2026-09-17: with no visible
// feedback at all, that boring-but-correct success looked identical to
// the button silently failing, since the table (already empty) doesn't
// visibly change either way.
document.getElementById('clear-symbol-switched-btn').addEventListener('click', async function () {
  if (!confirm('Permanently delete ALL symbol_switched closed-trade rows?')) return;
  const btn = this;
  const statusEl = document.getElementById('clear-status');
  btn.disabled = true;
  statusEl.textContent = '';
  statusEl.className = 'muted';
  try {
    const r = await fetch('/api/journal/clear_symbol_switched', { method: 'POST' });
    const body = await r.json();
    statusEl.textContent = body.deleted > 0
      ? 'cleared ' + body.deleted + ' row' + (body.deleted === 1 ? '' : 's')
      : 'nothing to clear';
  } catch (err) {
    statusEl.textContent = 'request failed';
    statusEl.className = 'neg';
  } finally {
    btn.disabled = false;
  }
  refresh();
});

// Pause/resume (via POST /api/polling) monitor-app's own applying of
// incoming bar-push events -- the EventSource connection below stays
// open either way (nothing to start/stop client-side, unlike the old
// setInterval poll), it just goes quiet because the server stops
// broadcasting while paused. Server truth (poll_enabled, from
// /api/state or /api/state/stream) is authoritative, not a client-only
// preference -- correct across multiple tabs/devices and across a fresh
// page load, with no localStorage needed.
let pollEnabled = true;

function applyPollUiState(enabled) {
  pollEnabled = enabled;
  document.getElementById('poll-toggle').textContent = enabled ? 'Pause updates' : 'Resume updates';
  document.getElementById('poll-status').textContent = enabled ? 'live' : 'paused';
}

document.getElementById('poll-toggle').addEventListener('click', async function () {
  const wantEnabled = !pollEnabled;
  let body;
  try {
    const r = await fetch('/api/polling', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: wantEnabled }),
    });
    body = await r.json();
  } catch (e) {
    return;  // request failed -- leave the UI showing the last known real state
  }
  applyPollUiState(body.poll_enabled);
  if (body.poll_enabled) refresh();  // immediate refresh on resume, not a wait for the next push
});

// Leg 2 of the poll -> push replacement (specs.md): pushes state updates
// the moment they change, via the browser's native EventSource API --
// replaces the old setInterval(refresh, POLL_MS) timer. No manual
// reconnect logic needed: EventSource retries per spec, including across
// a monitor-app restart, and each reconnect's first message is always a
// fresh full snapshot (see /api/state/stream), not a delta.
let evtSource = null;
function connectEventSource() {
  evtSource = new EventSource('/api/state/stream');
  evtSource.onmessage = function (e) {
    let data;
    try {
      data = JSON.parse(e.data);
    } catch (err) {
      return;
    }
    render(data);
  };
}
connectEventSource();
"""

_STYLE = """
:root{
  --bg:#0b0e14; --card:#141822; --border:#232838; --text:#e6e9ef; --muted:#8b93a7;
  --pos:#3ecf7e; --neg:#f0555a; --pending:#c9a227; --accent:#4f8cff;
}
*{box-sizing:border-box}
body{font:15px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
  margin:0;padding:1.5rem;background:var(--bg);color:var(--text);
  max-width:84rem;margin-inline:auto}
h1,h2,h3{margin:0 0 .35rem}
h2{font-size:.95rem;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
h3{font-size:.9rem;color:var(--text)}
.topbar{display:flex;align-items:center;justify-content:space-between;
  flex-wrap:wrap;gap:1rem;margin-bottom:1rem}
.poll-controls{display:flex;align-items:center;gap:.5rem;font-size:.85rem}
.poll-controls button{background:var(--card);color:var(--text);
  border:1px solid var(--border);border-radius:.4rem;padding:.4rem .8rem;
  cursor:pointer}
.watch-form{display:flex;align-items:center;gap:.6rem;flex-wrap:wrap;
  margin-bottom:1rem}
.watch-form input{background:var(--card);color:var(--text);
  border:1px solid var(--border);border-radius:.4rem;padding:.4rem .6rem;
  font-size:.9rem;width:12rem}
.watch-form button{background:var(--accent);color:#fff;border:none;
  border-radius:.4rem;padding:.4rem .9rem;cursor:pointer;font-size:.9rem}
#watch-status{font-size:.85rem}
#watch-status.neg{color:var(--neg);font-weight:600}
.banner{background:var(--pending);color:#1a1400;padding:.6rem 1rem;
  border-radius:.4rem;margin-bottom:1rem;font-weight:600}
/* Genuine 4-column grid at normal desktop widths, not auto-fit/auto-fill
   wrapping to 3 whenever columns want more room than the viewport has --
   that mismatch (22rem minimum column vs. the width 4 of them actually
   had available) was the real bug behind panels appearing to "go
   missing": the data was always there, a 4th panel was just wrapped
   below the fold. repeat(4, 1fr) forces the real column count instead of
   leaving it to chance; the two breakpoints below are where 4 genuinely-
   narrow columns (~15rem, still legible for this page's dense content)
   stop fitting, not arbitrary round numbers -- see specs.md section 5
   for the arithmetic. */
.grid{display:grid;grid-template-columns:repeat(4, 1fr);
  gap:.75rem;margin-bottom:1rem}
@media (max-width: 68rem){
  .grid{grid-template-columns:repeat(2, 1fr)}
}
@media (max-width: 38rem){
  .grid{grid-template-columns:1fr}
}
.card{background:var(--card);border:1px solid var(--border);
  border-radius:.6rem;padding:.75rem .9rem}
.hero{display:flex;align-items:baseline;gap:.6rem}
.hero-symbol{font-size:1.15rem;font-weight:700;letter-spacing:.02em}
.hero-price{font-size:1.9rem;font-weight:700;font-variant-numeric:tabular-nums;flex:1}
.remove-btn{background:transparent;color:var(--muted);
  border:1px solid var(--border);border-radius:.4rem;padding:.2rem .5rem;
  font-size:.72rem;cursor:pointer;align-self:center}
.remove-btn:hover{color:var(--neg);border-color:var(--neg)}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
table.detail th,table.detail td{padding:.2rem .4rem;text-align:left;
  border-bottom:1px solid var(--border);font-size:.82rem;line-height:1.25}
table.detail th{color:var(--muted);font-weight:500;width:45%}
.pos{color:var(--pos);font-weight:600}
.neg{color:var(--neg);font-weight:600}
.muted{color:var(--muted)}
.reverse-split-flag{color:var(--pending);font-weight:600;font-size:.82rem;margin:.15rem 0}
.zero-size-flag{color:var(--pending);font-weight:600;font-size:.72rem}
.continuation-flag{font-size:.82rem;margin:.15rem 0}
.continuation-flag-active{color:var(--pending);font-weight:600}
#current-equity{font-size:1.1rem;margin:0 0 .5rem}
.badge{display:inline-block;padding:.1rem .5rem;border-radius:1rem;
  font-size:.72rem;font-weight:600}
.badge-confirmed{background:rgba(62,207,126,.18);color:var(--pos)}
.badge-pending{background:rgba(201,162,39,.18);color:var(--pending)}
.setup-chips{display:flex;flex-wrap:wrap;gap:.3rem;margin:.3rem 0 .5rem}
.setup-chip{background:var(--bg);color:var(--text);border:1px solid var(--border);
  border-radius:1rem;padding:.2rem .6rem;font-size:.72rem;cursor:pointer}
.setup-chip:hover{border-color:var(--accent)}
.setup-chip:disabled{opacity:.5;cursor:default}
.setup-chip:disabled:hover{border-color:var(--border)}
.setup-detail{margin:.25rem 0 .5rem;padding:.4rem .5rem;
  background:var(--bg);border:1px solid var(--border);border-radius:.4rem}
.row-housekeeping td{color:var(--muted)}
.journal-delete-btn{padding:.15rem .45rem;font-size:.68rem}
.footer{color:var(--muted);font-size:.8rem;margin-top:1rem}
"""


def _wrap(body: str, poll_enabled: bool) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>momentum monitor</title>"
        f"<style>{_STYLE}</style></head><body>"
        f"<div class='topbar'><h1>momentum monitor</h1>{_poll_toggle_html(poll_enabled)}</div>"
        f"{body}"
        "<p class='footer'>Read-only technical read. Not advice, not an order.</p>"
        f"<script>{_SCRIPT}</script>"
        "</body></html>"
    )


def create_app(*, fetch_bars, watch_symbol=None,
               announce_watch=None, announce_unwatch=None, stream_events=None,
               announce_retry_attempts=ANNOUNCE_RETRY_ATTEMPTS,
               announce_retry_base_delay=ANNOUNCE_RETRY_BASE_DELAY_SECONDS,
               announce_retry_max_delay=ANNOUNCE_RETRY_MAX_DELAY_SECONDS,
               journal_store=None, trail_pct=DEFAULT_TRAIL_PCT,
               volume_confirm_threshold=DEFAULT_VOLUME_CONFIRM_THRESHOLD,
               base_equity=DEFAULT_BASE_EQUITY,
               risk_pct_per_trade=DEFAULT_RISK_PCT_PER_TRADE,
               swing_low_buffer_pct=DEFAULT_SWING_LOW_BUFFER_PCT,
               pattern_progress_threshold_pct=DEFAULT_PATTERN_PROGRESS_THRESHOLD_PCT,
               session_volume_multiple=DEFAULT_SESSION_VOLUME_MULTIPLE,
               continuation_lookback_days=DEFAULT_CONTINUATION_LOOKBACK_DAYS,
               continuation_threshold_pct=DEFAULT_CONTINUATION_THRESHOLD_PCT,
               fetch_daily_bars=None,
               now_fn=time.time, max_symbols=MAX_SYMBOLS) -> FastAPI:
    poller = Poller(fetch_bars=fetch_bars, watch_symbol=watch_symbol,
                    announce_watch=announce_watch,
                    announce_unwatch=announce_unwatch,
                    announce_retry_attempts=announce_retry_attempts,
                    announce_retry_base_delay=announce_retry_base_delay,
                    announce_retry_max_delay=announce_retry_max_delay,
                    journal_store=journal_store, trail_pct=trail_pct,
                    volume_confirm_threshold=volume_confirm_threshold,
                    base_equity=base_equity, risk_pct_per_trade=risk_pct_per_trade,
                    swing_low_buffer_pct=swing_low_buffer_pct,
                    pattern_progress_threshold_pct=pattern_progress_threshold_pct,
                    session_volume_multiple=session_volume_multiple,
                    continuation_lookback_days=continuation_lookback_days,
                    continuation_threshold_pct=continuation_threshold_pct,
                    fetch_daily_bars=fetch_daily_bars,
                    now_fn=now_fn, max_symbols=max_symbols)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Background tasks, not awaited here -- a slow (or, in a test,
        # deliberately blocked) initial catch-up fetch must not block the
        # app from starting up and serving requests (e.g. /api/state
        # showing "warming_up") while it's in flight, same non-blocking-
        # startup property the old poll loop had as a background task.
        run_task = asyncio.create_task(poller.run())
        stream_task = None
        if stream_events is not None:
            stream_task = asyncio.create_task(
                stream_events(poller.apply_bar_push, poller.resync_all))
        yield
        run_task.cancel()
        if stream_task is not None:
            stream_task.cancel()

    app = FastAPI(title="monitor-app", lifespan=lifespan)
    app.state.poller = poller

    @app.get("/api/state")
    async def api_state():
        return JSONResponse(poller._state_payload())

    @app.get("/api/state/stream")
    async def api_state_stream(request: Request):
        """Leg 2 of the poll -> push replacement (specs.md): replaces the
        browser's old 4s setInterval poll of GET /api/state. Same payload
        shape, pushed the instant Poller state changes instead of on a
        timer."""
        async def event_stream():
            async with poller.subscribe_state() as q:
                yield f"data: {json.dumps(poller._state_payload())}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        payload = await asyncio.wait_for(q.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    yield f"data: {json.dumps(payload)}\n\n"
        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.get("/", response_class=HTMLResponse)
    async def root():
        return _page(poller.all_full_states(), poller.recent_closed(limit=10),
                    poller.poll_enabled, poller.max_symbols, poller.strategy_params(),
                    poller.current_equity())

    @app.post("/api/watch")
    async def api_watch(request: Request):
        # Parsed manually (not via FastAPI's Form(...)) so this endpoint
        # doesn't need the python-multipart dependency: a plain HTML form
        # POST (no file inputs) is application/x-www-form-urlencoded, which
        # Starlette/urllib can already parse without it.
        body = (await request.body()).decode()
        parsed = parse_qs(body)
        symbol = (parsed.get("symbol") or [""])[0]
        # Optional "why watching" note in the SAME request (specs.md
        # section 7) -- empty/omitted is valid and normal, never blocks
        # the add. parse_qs drops a key entirely when its value is "",
        # so an explicitly-empty note and an omitted one both come back
        # as "" here -- both correctly mean "nothing to record."
        watch_note = (parsed.get("note") or [""])[0] or None
        ok, reason = await poller.add_symbol(symbol, watch_note)
        return JSONResponse({"ok": ok, "reason": reason, "symbols": poller.symbols},
                            status_code=200 if ok else 409)

    @app.post("/api/watch_note")
    async def api_watch_note(request: Request):
        # Updates the note for an ALREADY-watched symbol without
        # removing/re-adding it (specs.md section 7) -- JSON body, like
        # /api/polling, not tied to a plain HTML form.
        body = await request.json()
        symbol = body.get("symbol") or ""
        note = body.get("note") or ""
        ok, reason = poller.update_watch_note(symbol, note)
        return JSONResponse({"ok": ok, "reason": reason}, status_code=200 if ok else 409)

    @app.get("/api/reverse_splits")
    async def api_reverse_splits_get(symbol: str):
        # Checkable for ANY symbol, watched or not (specs.md section 7) --
        # the flag's main value is informing the decision to watch
        # something in the first place.
        return JSONResponse({"symbol": symbol.strip().upper(),
                             "splits": poller.reverse_splits_for(symbol)})

    @app.post("/api/reverse_splits")
    async def api_reverse_splits_post(request: Request):
        # {"symbol", "split_date", "ratio", "note"} -- JSON body, like
        # /api/watch_note, not tied to a plain HTML form. A curated,
        # manually-entered flag (specs.md section 7): no live data source
        # for reverse-split history exists via Schwab.
        body = await request.json()
        symbol = body.get("symbol") or ""
        split_date = body.get("split_date") or ""
        ratio = body.get("ratio") or ""
        note = body.get("note") or None
        ok, reason = poller.add_reverse_split(symbol, split_date, ratio, note)
        return JSONResponse({"ok": ok, "reason": reason}, status_code=200 if ok else 409)

    @app.post("/api/unwatch")
    async def api_unwatch(request: Request):
        body = (await request.body()).decode()
        symbol = (parse_qs(body).get("symbol") or [""])[0]
        removed = await poller.remove_symbol(symbol)
        return JSONResponse({"removed": removed, "symbols": poller.symbols})

    @app.post("/api/polling")
    async def api_polling(request: Request):
        # Pauses/resumes monitor-app's own applying of incoming bar-push
        # events (the thing that actually reflects schwab-connector's
        # data) -- distinct from, and more important than, the browser's
        # EventSource connection, which only carries browser<->monitor-app
        # traffic. JSON body, not a form: this is only ever called from
        # the page's own JS, never submitted as an HTML form.
        body = await request.json()
        await poller.set_poll_enabled(bool(body.get("enabled", True)))
        return JSONResponse({"poll_enabled": poller.poll_enabled})

    @app.post("/api/journal/delete")
    async def api_journal_delete(request: Request):
        # Permanently deletes one CLOSED trade row -- the page's own JS
        # gates this behind a confirm() dialog before ever calling it
        # (same "never silently delete" standard as everything else in
        # this project), but the endpoint itself doesn't re-implement that
        # UI-level confirmation; it trusts the caller already got it.
        body = (await request.body()).decode()
        raw_id = (parse_qs(body).get("id") or [""])[0]
        try:
            trade_id = int(raw_id)
        except ValueError:
            return JSONResponse(
                {"ok": False, "reason": f"{raw_id!r} is not a valid trade id"},
                status_code=409,
            )
        deleted = poller.delete_closed_trade(trade_id)
        return JSONResponse({"ok": deleted}, status_code=200 if deleted else 404)

    @app.post("/api/journal/clear_symbol_switched")
    async def api_journal_clear_symbol_switched():
        deleted = poller.clear_symbol_switched()
        return JSONResponse({"ok": True, "deleted": deleted})

    @app.get("/api/strategy_params")
    async def api_strategy_params_get():
        # Live-tunable strategy parameters (specs.md section 8): current
        # value + when each was last changed. "history" is the full
        # change log (most recent first), included here rather than a
        # separate route -- there's only ever a handful of parameters and
        # a modest number of changes, not worth a second round trip for.
        return JSONResponse({
            "params": poller.strategy_params(),
            "history": poller.strategy_param_history(),
        })

    @app.post("/api/strategy_params")
    async def api_strategy_params_post(request: Request):
        # {"trail_pct": 0.08} or {"trail_pct": 0.08, "volume_confirm_threshold": 2.0}
        # -- one or more keys in a single call. Each is validated
        # independently (journal_store.InvalidParamError -> 409, same
        # convention as every other validation failure in this app); a
        # rejected key changes NOTHING for that key (set_param never
        # writes on failure) and the response reports exactly which keys
        # succeeded vs. were rejected, rather than all-or-nothing failing
        # the whole call over one bad value among several.
        body = await request.json()
        updated = {}
        rejected = {}
        for key, value in body.items():
            try:
                poller.set_strategy_param(key, float(value))
                updated[key] = value
            except (InvalidParamError, TypeError, ValueError) as exc:
                rejected[key] = str(exc)
        status = 200 if not rejected else 409
        return JSONResponse(
            {"ok": not rejected, "updated": updated, "rejected": rejected,
             "params": poller.strategy_params()},
            status_code=status,
        )

    @app.get("/api/equity")
    async def api_equity_get():
        # current_equity is a running value, not a simple param (specs.md
        # section 7) -- its own endpoint, distinct from strategy_params,
        # since base_equity/risk_pct_per_trade (the live-tunable INPUTS to
        # sizing) already live there via the existing mechanism.
        return JSONResponse({
            "current_equity": poller.current_equity(),
            "history": poller.equity_history(),
        })

    @app.post("/api/equity/reset")
    async def api_equity_reset():
        # Sets current_equity to the LIVE base_equity param -- a distinct
        # explicit action from an override below, logged as "manual_reset"
        # so it's never confused with a trade-driven change later.
        ok, result = poller.reset_equity()
        if not ok:
            return JSONResponse({"ok": False, "reason": result}, status_code=409)
        return JSONResponse({"ok": True, "current_equity": result})

    @app.post("/api/equity/override")
    async def api_equity_override(request: Request):
        # {"value": 1500.0} -- sets current_equity directly, WITHOUT
        # touching base_equity (a future reset still targets the
        # unchanged base_equity) -- for correcting a mistake or
        # deliberately starting from a different number. Rejects a
        # non-positive value, same 409 convention as every other
        # validation failure in this app.
        body = await request.json()
        try:
            value = float(body.get("value"))
        except (TypeError, ValueError):
            return JSONResponse(
                {"ok": False, "reason": "value must be a number"}, status_code=409)
        ok, reason = poller.override_equity(value)
        return JSONResponse({"ok": ok, "reason": reason}, status_code=200 if ok else 409)

    return app
