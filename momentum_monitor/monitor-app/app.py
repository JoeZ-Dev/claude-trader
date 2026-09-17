"""
monitor-app FastAPI web app -- phase 2: up to MAX_SYMBOLS (4) concurrently
watched symbols, each independently polled, analyzed, and journaled.

Holds no credentials. A background poller pulls new bars from
schwab-connector for every currently-watched symbol, keeps each symbol's
own running bar list, and recomputes that symbol's full state
(state.build_state, UNCHANGED -- phase 2 runs the exact same per-symbol
math N times, not different math) after every poll. Serves:

  GET  /api/state    -> {"symbols": {SYM: {...same shape as phase 1's
                        whole response, plus a per-symbol "journal.open"},
                        ...}, "recent_closed": [...], "poll_enabled": bool,
                        "max_symbols": int}
                        Deliberate breaking change from phase 1's single-
                        object shape -- nothing else depends on the old
                        form, no back-compat shim.
  GET  /             -> Stage B: a responsive grid of up to 4 symbol
                        panels, an add-symbol form (POSTs /api/watch,
                        fills the next empty slot -- never replaces an
                        existing one), and a remove control on each panel
                        (POSTs /api/unwatch for that panel's own symbol
                        only). JS-refreshed in place the same way as
                        phase 1 (no meta-refresh, no full-page reload):
                        refresh() re-fetches /api/state and rebuilds
                        #symbols' innerHTML each poll.
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
                        background poll loop for ALL watched symbols at
                        once (one global switch, not per-symbol) -- see
                        Poller.set_poll_enabled.
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
needs no schwab-connector changes, per specs.md's own roadmap note. One
architectural fact worth knowing, not a defect: each watched symbol gets
its own independent Schwab streaming connection and its own independent
~30-minute proactive token-refresh cycle (main.py's _source_factory builds
a fresh ReconnectingStreamSource, with its own AccessTokenSource, per
symbol) rather than one connection multiplexing many symbols -- 4
concurrent symbols means 4 independent WebSocket sessions and 4
independent refresh cycles. This needs to be watched under real load, not
assumed to be fine because it's not a "single-symbol assumption" bug (see
the phase 2 live-proof evidence for whether it actually holds up).
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from journal_logic import ExitEvent, OpenPosition, advance_journal
from state import build_state

logger = logging.getLogger(__name__)

DEFAULT_TRAIL_PCT = 0.05
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
    per-symbol object. `journal_position`/`journal_was_confirmed` being
    per-slot (not per-Poller) is exactly what makes "at most one open
    position PER symbol" (rather than one globally) correct."""
    symbol: str
    bars: list[dict] = field(default_factory=list)
    last_ts: float = 0.0
    state: dict = field(default_factory=dict)
    poll_ok: bool = False
    journal_position: OpenPosition | None = None
    journal_was_confirmed: bool = False


class Poller:
    def __init__(self, *, fetch_bars, watch_symbol=None, poll_interval, announce_watch,
                 announce_unwatch=None,
                 announce_retry_attempts=ANNOUNCE_RETRY_ATTEMPTS,
                 announce_retry_base_delay=ANNOUNCE_RETRY_BASE_DELAY_SECONDS,
                 announce_retry_max_delay=ANNOUNCE_RETRY_MAX_DELAY_SECONDS,
                 journal_store=None, trail_pct=DEFAULT_TRAIL_PCT, now_fn=time.time,
                 max_symbols=MAX_SYMBOLS):
        self._fetch_bars = fetch_bars
        self._initial_symbol = watch_symbol.upper() if watch_symbol else None
        self._interval = poll_interval
        self._announce_watch = announce_watch
        self._announce_unwatch = announce_unwatch
        self._announce_retry_attempts = announce_retry_attempts
        self._announce_retry_base_delay = announce_retry_base_delay
        self._announce_retry_max_delay = announce_retry_max_delay
        self._journal_store = journal_store
        self._trail_pct = trail_pct
        self._now_fn = now_fn
        self._max_symbols = max_symbols
        self._slots: dict[str, _SymbolSlot] = {}
        self._poll_enabled = True

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

    def set_poll_enabled(self, enabled: bool) -> None:
        """Pause/resume the background poll loop for ALL watched symbols
        at once (one global switch, not per-symbol) -- schwab-connector's
        live stream(s) and stored bars are unaffected either way, this
        only stops monitor-app from pulling new ones in while nobody's
        watching. Distinct from (and the thing that actually matters,
        unlike) the client-side setInterval the page also has -- that only
        stops browser<->monitor-app traffic, which never left localhost/
        the LAN in the first place."""
        self._poll_enabled = enabled

    def state_for(self, symbol: str) -> dict | None:
        slot = self._slots.get(symbol.upper())
        return slot.state if slot else None

    def full_state_for(self, symbol: str) -> dict | None:
        """state.build_state's output for `symbol`, plus that symbol's OWN
        journal open-position block. Does NOT include recent_closed, which
        is intentionally cross-symbol -- see recent_closed()."""
        slot = self._slots.get(symbol.upper())
        if slot is None:
            return None
        payload = dict(slot.state)
        payload["journal"] = {"open": self._journal_open_for(slot)}
        return payload

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
        return self._journal_store.delete_closed(trade_id)

    def clear_symbol_switched(self) -> int:
        """Permanently deletes every symbol_switched closed row (the bulk
        housekeeping-cleanup action, specs.md section 6). Returns the
        number removed."""
        if self._journal_store is None:
            return 0
        return self._journal_store.delete_symbol_switched()

    def _journal_open_for(self, slot: _SymbolSlot) -> dict | None:
        if slot.journal_position is None:
            return None
        last_price = (slot.bars[-1]["close"] if slot.bars
                     else slot.journal_position.entry_price)
        unrealized_pct = ((last_price - slot.journal_position.entry_price)
                          / slot.journal_position.entry_price * 100.0)
        return {
            "symbol": slot.journal_position.symbol,
            "entry_price": round(slot.journal_position.entry_price, 4),
            "stop_level": round(slot.journal_position.stop_level, 4),
            "unrealized_pnl_pct": round(unrealized_pct, 4),
        }

    async def run(self) -> None:
        if self._initial_symbol is not None:
            await self.add_symbol(self._initial_symbol)
        while True:
            if self._poll_enabled:
                for symbol in list(self._slots.keys()):
                    await self._poll_once(symbol)
            await asyncio.sleep(self._interval)

    async def add_symbol(self, symbol: str) -> tuple[bool, str]:
        """Add a symbol to the watched set. Returns (True, note) on
        success -- `note` is "" normally, or a human-readable line saying
        what got evicted when the set was already full. Returns (False,
        reason) on rejection -- an invalid symbol or one already watched,
        each with its own clear reason. Never a silent failure (phase 1's
        switch_symbol silently replaced whatever was watched; phase 2
        never does that for an UNRELATED slot).

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

        note = ""
        if len(self._slots) >= self._max_symbols:
            oldest = next(iter(self._slots))
            await self.remove_symbol(oldest)
            note = f"dropped {oldest} (oldest) to make room for {symbol}"

        self._slots[symbol] = _SymbolSlot(symbol=symbol, state=build_state([], symbol))
        if self._journal_store is not None:
            # Resume an already-open position for this symbol (a restart,
            # or re-adding something with a position still open) rather
            # than losing track of it. Seeding journal_was_confirmed to
            # True when one exists prevents a spurious duplicate-entry
            # attempt on the very next poll.
            resumed = self._journal_store.open_position_for(symbol)
            self._slots[symbol].journal_position = resumed
            self._slots[symbol].journal_was_confirmed = resumed is not None
        if self._announce_watch is not None:
            await self._announce_watch_with_retry(symbol)
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
        return True

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

    async def _poll_once(self, symbol: str) -> None:
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
        resistance = slot.state.get("levels", {}).get("resistance")
        is_confirmed_now = bool(resistance and resistance["hold"]["confirmed"])
        tick = advance_journal(
            position=slot.journal_position, new_bars=new_bars,
            is_confirmed_now=is_confirmed_now,
            was_confirmed_before=slot.journal_was_confirmed,
            trail_pct=self._trail_pct, symbol=symbol,
        )
        if tick.opened is not None:
            slot.journal_position = self._journal_store.create(tick.opened)
        elif tick.updated is not None:
            self._journal_store.update_trailing(tick.updated)
            slot.journal_position = tick.updated
        elif tick.closed is not None:
            position, exit_event = tick.closed
            self._journal_store.close_position(position, exit_event)
            slot.journal_position = None
        slot.journal_was_confirmed = tick.was_confirmed_after


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


def _journal_open_html(open_block: dict | None) -> str:
    if open_block is None:
        return "<p class='muted'>No open virtual position.</p>"
    cls = _sign_class(open_block["unrealized_pnl_pct"])
    return (
        "<table class='detail'>"
        f"<tr><th>symbol</th><td>{html.escape(str(open_block['symbol']))}</td></tr>"
        f"<tr><th>entry price</th><td>{_fmt(open_block['entry_price'], 2)}</td></tr>"
        f"<tr><th>trailing stop</th><td>{_fmt(open_block['stop_level'], 2)}</td></tr>"
        f"<tr><th>unrealized P&amp;L %</th>"
        f"<td class='{cls}'>{_fmt(open_block['unrealized_pnl_pct'], 2)}%</td></tr>"
        "</table>"
    )


def _journal_closed_rows_html(closed: list[dict]) -> str:
    if not closed:
        return "<tr><td colspan='6' class='muted'>No closed trades yet.</td></tr>"
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
        pnl = "" if t["realized_pnl_pct"] is None else f"{_fmt(t['realized_pnl_pct'], 2)}%"
        row_open = "<tr class='row-housekeeping'>" if is_housekeeping else "<tr>"
        rows.append(
            f"{row_open}"
            f"<td>{html.escape(str(t['symbol']))}</td>"
            f"<td>{_fmt(t['entry_price'], 2)}</td><td>{_fmt(t['exit_price'], 2)}</td>"
            f"<td>{html.escape(str(t['exit_reason']))}</td>"
            f"<td class='{cls}'>{pnl}</td>"
            "<td><button type='button' class='remove-btn journal-delete-btn' "
            f"data-trade-id='{t['id']}'>delete</button></td>"
            "</tr>"
        )
    return "".join(rows)


def _remove_button_html(symbol: str) -> str:
    sym = html.escape(symbol)
    return f"<button type='button' class='remove-btn' data-symbol='{sym}'>remove</button>"


def _symbol_card_html(symbol: str, state: dict) -> str:
    """One grid panel per watched symbol: the same per-block renderers
    phase 1's single-symbol page used, plus a remove control scoped to
    this panel's own symbol (data-symbol, wired via event delegation on
    #symbols in _SCRIPT -- see refresh())."""
    sym = html.escape(symbol)
    if state.get("status") != "ok":
        msg = (f"Warming up — waiting for bars for {sym}." if state.get("symbol")
              else "No data yet.")
        return (f"<section class='card' data-symbol='{sym}'>"
                f"<div class='hero'><h2>{sym}</h2>{_remove_button_html(symbol)}</div>"
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
    return f"""
<form id="watch-form" class="watch-form">
  <input type="text" id="watch-input" placeholder="Add symbol (e.g. NVDA)" maxlength="10" autocomplete="off">
  <button type="submit">Add</button>
  <span id="slot-count" class="muted">{count} / {max_symbols} symbols watched</span>
  <span id="watch-status" class="muted"></span>
</form>
"""


def _page(full_states: dict[str, dict], recent_closed: list[dict], poll_enabled: bool,
          max_symbols: int) -> str:
    if full_states:
        cards_html = "".join(_symbol_card_html(sym, st) for sym, st in full_states.items())
    else:
        cards_html = ("<p class='muted'>No symbols watched yet — add one below "
                      f"(up to {max_symbols}).</p>")

    body = f"""
{_watch_form_html(len(full_states), max_symbols)}
<div id="symbols" class="grid">{cards_html}</div>
<section class="card">
  <div class="hero">
    <h2>Recent closed trades</h2>
    <button type="button" id="clear-symbol-switched-btn" class="remove-btn">clear symbol_switched rows</button>
    <span id="clear-status" class="muted"></span>
  </div>
  <table class="detail">
    <tr><th>symbol</th><th>entry</th><th>exit</th><th>reason</th><th>P&amp;L %</th><th></th></tr>
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
function journalOpenHtml(open) {
  if (!open) return '<p class="muted">No open virtual position.</p>';
  const cls = signClass(open.unrealized_pnl_pct);
  return '<table class="detail">' +
    '<tr><th>symbol</th><td>' + esc(open.symbol) + '</td></tr>' +
    '<tr><th>entry price</th><td>' + fmt(open.entry_price, 2) + '</td></tr>' +
    '<tr><th>trailing stop</th><td>' + fmt(open.stop_level, 2) + '</td></tr>' +
    '<tr><th>unrealized P&amp;L %</th><td class="' + cls + '">' + fmt(open.unrealized_pnl_pct, 2) + '%</td></tr>' +
    '</table>';
}
function journalClosedRows(closed) {
  if (!closed || !closed.length) {
    return '<tr><td colspan="6" class="muted">No closed trades yet.</td></tr>';
  }
  return closed.map(function(t) {
    // symbol_switched = watchlist housekeeping, not a trading outcome --
    // see _journal_closed_rows_html's comment (Python side) for why the
    // whole row is muted, overriding pos/neg P&L coloring too.
    const isHousekeeping = t.exit_reason === 'symbol_switched';
    const cls = isHousekeeping ? 'muted' : signClass(t.realized_pnl_pct);
    const pnl = t.realized_pnl_pct === null ? '' : fmt(t.realized_pnl_pct, 2) + '%';
    const rowOpen = isHousekeeping ? '<tr class="row-housekeeping">' : '<tr>';
    return rowOpen + '<td>' + esc(t.symbol) + '</td><td>' + fmt(t.entry_price, 2) + '</td>' +
      '<td>' + fmt(t.exit_price, 2) + '</td><td>' + esc(t.exit_reason) + '</td>' +
      '<td class="' + cls + '">' + pnl + '</td>' +
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
function symbolCardHtml(symbol, state) {
  const sym = esc(symbol);
  if (state.status !== 'ok') {
    return '<section class="card" data-symbol="' + sym + '">' +
      '<div class="hero"><h2>' + sym + '</h2>' + removeButtonHtml(symbol) + '</div>' +
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
async function refresh() {
  let data;
  try {
    const r = await fetch('/api/state');
    data = await r.json();
  } catch (e) {
    return;  // keep showing the last-good render, same philosophy as the server's own poll_ok
  }
  applyPollUiState(data.poll_enabled);  // stay in sync even if another tab paused/resumed it
  const symbolsEl = document.getElementById('symbols');

  // Preserve which setup-chip/level-chip detail sections are currently
  // expanded across this poll's full innerHTML rebuild -- fixed
  // 2026-09-17: without this, an expanded section silently re-collapsed
  // on the very next ~4s poll, since #symbols' whole subtree gets
  // rebuilt from scratch every refresh() and a freshly-built chip always
  // starts hidden, with nothing remembering which ones were open.
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
  const statusEl = document.getElementById('watch-status');
  const symbol = input.value.trim();
  if (!symbol) return;
  statusEl.textContent = '';
  statusEl.className = 'muted';
  let body;
  try {
    const r = await fetch('/api/watch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: 'symbol=' + encodeURIComponent(symbol),
    });
    body = await r.json();
  } catch (err) {
    statusEl.textContent = 'request failed';
    statusEl.className = 'neg';
    return;
  }
  if (body.ok) {
    input.value = '';
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
// control below. Expanded state is NOT preserved across the next poll
// (refresh() rebuilds #symbols' innerHTML from scratch every 4s, same as
// every other in-place update this page does) -- an accepted tradeoff,
// not an oversight, consistent with this page's existing "no persistent
// client state across polls" design.
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

// Pause/resume BOTH the client-side display loop AND (via POST
// /api/polling) monitor-app's own server-side poller -- the client loop
// alone only stops browser<->monitor-app traffic, which never left
// localhost/the LAN; the server-side one is what actually stops hitting
// schwab-connector. Server truth (poll_enabled, from /api/state) is
// authoritative, not a client-only preference -- correct across multiple
// tabs/devices and across a fresh page load, with no localStorage needed.
const POLL_MS = 4000;
let pollTimer = null;
let pollEnabled = true;

function applyPollUiState(enabled) {
  pollEnabled = enabled;
  document.getElementById('poll-toggle').textContent = enabled ? 'Pause updates' : 'Resume updates';
  document.getElementById('poll-status').textContent = enabled ? 'live' : 'paused';
  if (enabled) {
    if (!pollTimer) pollTimer = setInterval(refresh, POLL_MS);
  } else if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
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
  if (body.poll_enabled) refresh();  // immediate refresh on resume, not a wait for the next tick
});

refresh();
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


def create_app(*, fetch_bars, watch_symbol=None, poll_interval: float = 5.0,
               announce_watch=None, announce_unwatch=None,
               announce_retry_attempts=ANNOUNCE_RETRY_ATTEMPTS,
               announce_retry_base_delay=ANNOUNCE_RETRY_BASE_DELAY_SECONDS,
               announce_retry_max_delay=ANNOUNCE_RETRY_MAX_DELAY_SECONDS,
               journal_store=None, trail_pct=DEFAULT_TRAIL_PCT,
               now_fn=time.time, max_symbols=MAX_SYMBOLS) -> FastAPI:
    poller = Poller(fetch_bars=fetch_bars, watch_symbol=watch_symbol,
                    poll_interval=poll_interval, announce_watch=announce_watch,
                    announce_unwatch=announce_unwatch,
                    announce_retry_attempts=announce_retry_attempts,
                    announce_retry_base_delay=announce_retry_base_delay,
                    announce_retry_max_delay=announce_retry_max_delay,
                    journal_store=journal_store, trail_pct=trail_pct,
                    now_fn=now_fn, max_symbols=max_symbols)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        task = asyncio.create_task(poller.run())
        yield
        task.cancel()

    app = FastAPI(title="monitor-app", lifespan=lifespan)
    app.state.poller = poller

    @app.get("/api/state")
    async def api_state():
        return JSONResponse({
            "symbols": poller.all_full_states(),
            "recent_closed": poller.recent_closed(limit=10),
            "poll_enabled": poller.poll_enabled,
            "max_symbols": poller.max_symbols,
        })

    @app.get("/", response_class=HTMLResponse)
    async def root():
        return _page(poller.all_full_states(), poller.recent_closed(limit=10),
                    poller.poll_enabled, poller.max_symbols)

    @app.post("/api/watch")
    async def api_watch(request: Request):
        # Parsed manually (not via FastAPI's Form(...)) so this endpoint
        # doesn't need the python-multipart dependency: a plain HTML form
        # POST (no file inputs) is application/x-www-form-urlencoded, which
        # Starlette/urllib can already parse without it.
        body = (await request.body()).decode()
        symbol = (parse_qs(body).get("symbol") or [""])[0]
        ok, reason = await poller.add_symbol(symbol)
        return JSONResponse({"ok": ok, "reason": reason, "symbols": poller.symbols},
                            status_code=200 if ok else 409)

    @app.post("/api/unwatch")
    async def api_unwatch(request: Request):
        body = (await request.body()).decode()
        symbol = (parse_qs(body).get("symbol") or [""])[0]
        removed = await poller.remove_symbol(symbol)
        return JSONResponse({"removed": removed, "symbols": poller.symbols})

    @app.post("/api/polling")
    async def api_polling(request: Request):
        # Pauses/resumes monitor-app's own background poll loop (the thing
        # that actually hits schwab-connector) -- distinct from, and more
        # important than, the client-side setInterval toggle, which only
        # stops browser<->monitor-app traffic. JSON body, not a form: this
        # is only ever called from the page's own JS, never submitted as an
        # HTML form.
        body = await request.json()
        poller.set_poll_enabled(bool(body.get("enabled", True)))
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

    return app
