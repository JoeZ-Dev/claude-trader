"""
monitor-app FastAPI web app.

Holds no credentials. A background poller pulls new bars from
schwab-connector, keeps the running bar list, and recomputes the full
state (state.build_state) after every poll. Serves:

  GET  /api/state  -> the state dict (JSON), verbatim
  GET  /           -> a plain auto-refreshing HTML view of the same data,
                      with a ticker text box for switching symbols
  POST /api/watch  -> {symbol: "..."} (urlencoded form) switches which
                      symbol the poller watches, without a restart -- see
                      Poller.switch_symbol, which also unwatches whatever
                      symbol was previously watched (via announce_unwatch)
                      so switching doesn't accumulate watched symbols on
                      schwab-connector indefinitely, and force-closes any
                      open virtual-journal position for the symbol being
                      left (see journal_logic.py / journal_store.py).
                      Redirects back to / (303).

create_app() takes fetch_bars / announce_watch / announce_unwatch as
callables so tests inject fakes; main.py binds them to httpx calls against
schwab-connector. journal_store (optional -- a journal_store.JournalStore)
wires in the phase-4 virtual trade journal (specs.md section 6); passing
None disables it entirely (no entry/exit tracking, no journal section on
the page).
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from journal_logic import ExitEvent, advance_journal
from state import build_state

logger = logging.getLogger(__name__)

DEFAULT_TRAIL_PCT = 0.05

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


class Poller:
    def __init__(self, *, fetch_bars, watch_symbol, poll_interval, announce_watch,
                 announce_unwatch=None,
                 announce_retry_attempts=ANNOUNCE_RETRY_ATTEMPTS,
                 announce_retry_base_delay=ANNOUNCE_RETRY_BASE_DELAY_SECONDS,
                 announce_retry_max_delay=ANNOUNCE_RETRY_MAX_DELAY_SECONDS,
                 journal_store=None, trail_pct=DEFAULT_TRAIL_PCT, now_fn=time.time):
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
        self._symbol: str | None = None
        self._bars: list[dict] = []
        self._last_ts: float = 0.0
        self._state: dict = build_state([], None)
        self._poll_ok = False
        self._journal_position = None   # journal_logic.OpenPosition | None
        self._journal_was_confirmed = False
        self._poll_enabled = True

    @property
    def state(self) -> dict:
        return self._state

    @property
    def poll_enabled(self) -> bool:
        return self._poll_enabled

    def set_poll_enabled(self, enabled: bool) -> None:
        """Pause/resume the background poll loop itself (not just what the
        browser displays) -- schwab-connector's live stream and stored bars
        are unaffected either way, this only stops monitor-app from pulling
        new ones in while nobody's watching. Distinct from (and the thing
        that actually matters, unlike) the client-side setInterval the page
        also has -- that only stops browser<->monitor-app traffic, which
        never left localhost/the LAN in the first place."""
        self._poll_enabled = enabled

    def journal_snapshot(self) -> dict:
        """Everything the page/API need to show the journal: the open
        position (if any), with a live unrealized P&L computed against the
        current last price, plus recent closed trades (all symbols, most
        recent first -- specs.md: "for end-of-day review", not scoped to
        just the currently-watched symbol)."""
        if self._journal_store is None:
            return {"open": None, "recent_closed": []}
        open_block = None
        if self._journal_position is not None:
            last_price = (self._bars[-1]["close"] if self._bars
                         else self._journal_position.entry_price)
            unrealized_pct = ((last_price - self._journal_position.entry_price)
                              / self._journal_position.entry_price * 100.0)
            open_block = {
                "symbol": self._journal_position.symbol,
                "entry_price": round(self._journal_position.entry_price, 4),
                "stop_level": round(self._journal_position.stop_level, 4),
                "unrealized_pnl_pct": round(unrealized_pct, 4),
            }
        return {
            "open": open_block,
            "recent_closed": self._journal_store.recent_closed(limit=10),
        }

    async def run(self) -> None:
        if self._initial_symbol is not None:
            await self.switch_symbol(self._initial_symbol)
        while True:
            if self._symbol is not None and self._poll_enabled:
                await self._poll_once()
            await asyncio.sleep(self._interval)

    async def switch_symbol(self, new_symbol: str) -> bool:
        """Start watching a different symbol, replacing whatever this poller
        was previously watching -- the runtime equivalent of what WATCH_SYMBOL
        used to require a container restart for. Resets the accumulated bar
        history and re-announces the watch to schwab-connector (which itself
        backfills the new symbol's session -- see price_history.py -- so
        switching gets the same correct-from-market-open behavior a fresh
        watch always has, not a second-class cold start). Also unwatches
        whatever symbol was previously being watched, restoring the
        one-symbol-at-a-time invariant -- without this, schwab-connector
        accumulates every symbol ever typed into the ticker box forever
        (confirmed live: 6 simultaneously-watched symbols from normal use
        of this box before this was fixed).

        A blank/invalid symbol, or the symbol already being watched, is a
        silent no-op -- returns False. Returns True if it actually switched.

        Also force-closes any open virtual-journal position for the OLD
        symbol at its last known price, exit_reason="symbol_switched" --
        distinct from "trailing_stop" so later review doesn't conflate "the
        trade stopped out" with "the user just moved on." A position left
        open with no further price updates could never resolve otherwise."""
        new_symbol = new_symbol.strip().upper()
        if not new_symbol or new_symbol == self._symbol or not _VALID_SYMBOL.match(new_symbol):
            return False
        old_symbol = self._symbol
        if (old_symbol is not None and self._journal_store is not None
                and self._journal_position is not None):
            last_bar = self._bars[-1] if self._bars else None
            exit_price = (last_bar["close"] if last_bar is not None
                         else self._journal_position.entry_price)
            exit_ts = last_bar["ts"] if last_bar is not None else int(self._now_fn())
            self._journal_store.close_position(
                self._journal_position,
                ExitEvent(exit_ts=exit_ts, exit_price=exit_price,
                         exit_reason="symbol_switched"),
            )
            self._journal_position = None
        self._symbol = new_symbol
        self._bars = []
        self._last_ts = 0.0
        self._poll_ok = False
        self._state = build_state([], new_symbol)
        if self._announce_watch is not None:
            await self._announce_watch_with_retry()
        if old_symbol is not None and self._announce_unwatch is not None:
            # Best-effort, unlike announce_watch's retries: a failure here
            # just leaves one stale symbol watched on schwab-connector
            # (annoying, not broken -- the new symbol above is what's
            # actually displayed and it's already watched), so it isn't
            # worth delaying the switch the user is actively waiting on.
            try:
                await self._announce_unwatch(old_symbol)
            except Exception as exc:
                logger.warning(
                    "announce_unwatch(%s) failed; schwab-connector will keep "
                    "watching it until explicitly unwatched again: %s",
                    old_symbol, exc,
                )
        if self._journal_store is not None:
            # Resume an already-open position for the new symbol (a
            # restart, or switching back to something with a position
            # still open) rather than losing track of it. Seeding
            # _journal_was_confirmed to True when one exists prevents a
            # spurious duplicate-entry attempt on the very next poll.
            self._journal_position = self._journal_store.open_position_for(new_symbol)
        self._journal_was_confirmed = self._journal_position is not None
        return True

    async def _announce_watch_with_retry(self) -> None:
        delay = self._announce_retry_base_delay
        for attempt in range(1, self._announce_retry_attempts + 1):
            try:
                await self._announce_watch(self._symbol)
                return
            except Exception as exc:
                if attempt < self._announce_retry_attempts:
                    logger.warning(
                        "announce_watch(%s) failed on attempt %d/%d, "
                        "retrying in %.1fs: %s",
                        self._symbol, attempt, self._announce_retry_attempts,
                        delay, exc,
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, self._announce_retry_max_delay)
                else:
                    logger.error(
                        "announce_watch(%s) failed on all %d attempts; "
                        "schwab-connector will never register this symbol "
                        "as watched unless something else calls POST /watch: %s",
                        self._symbol, self._announce_retry_attempts, exc,
                    )

    async def _poll_once(self) -> None:
        symbol = self._symbol
        try:
            incoming = await self._fetch_bars(symbol, self._last_ts)
        except Exception:
            self._poll_ok = False  # keep serving the last good state
            return
        if symbol != self._symbol:
            # switch_symbol() landed while this fetch was in flight -- these
            # bars belong to the symbol we just switched AWAY from. Applying
            # them now would corrupt the new symbol's freshly-reset series.
            return
        new_bars = []
        for bar in incoming:
            if not self._bars or bar["ts"] > self._bars[-1]["ts"]:
                self._bars.append(bar)
                new_bars.append(bar)
        if new_bars:
            self._last_ts = self._bars[-1]["ts"]
        self._state = build_state(self._bars, symbol)
        self._poll_ok = True
        self._update_journal(symbol, new_bars)

    def _update_journal(self, symbol: str, new_bars: list[dict]) -> None:
        if self._journal_store is None:
            return
        if self._journal_position is not None:
            # A resumed position (a restart, or switching back to a symbol
            # with one still open) can have new_bars containing history
            # from BEFORE its entry -- self._last_ts resets to 0.0 on a
            # restart/switch, so the full session gets refetched as if it
            # were all new. Exit-checking must never see pre-entry bars as
            # if they happened after entry (confirmed by a real bug this
            # caught: a resumed position was being phantom-stopped-out
            # against its own pre-entry price history on the very next
            # poll after a restart).
            new_bars = [b for b in new_bars if b["ts"] > self._journal_position.entry_ts]
        resistance = self._state.get("levels", {}).get("resistance")
        is_confirmed_now = bool(resistance and resistance["hold"]["confirmed"])
        tick = advance_journal(
            position=self._journal_position, new_bars=new_bars,
            is_confirmed_now=is_confirmed_now,
            was_confirmed_before=self._journal_was_confirmed,
            trail_pct=self._trail_pct, symbol=symbol,
        )
        if tick.opened is not None:
            self._journal_position = self._journal_store.create(tick.opened)
        elif tick.updated is not None:
            self._journal_store.update_trailing(tick.updated)
            self._journal_position = tick.updated
        elif tick.closed is not None:
            position, exit_event = tick.closed
            self._journal_store.close_position(position, exit_event)
            self._journal_position = None
        self._journal_was_confirmed = tick.was_confirmed_after


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


def _level_block_html(title: str, block: dict | None) -> str:
    if block is None:
        return f"<h3>{html.escape(title)}</h3><p class='muted'>none on this side of price</p>"
    c, h = block["components"], block["hold"]
    badge = (
        "<span class='badge badge-confirmed'>confirmed</span>" if h["confirmed"]
        else "<span class='badge badge-pending'>not confirmed</span>"
    )
    return (
        f"<h3>{html.escape(title)} @ {_fmt(block['price'], 2)}</h3>"
        "<table class='detail'>"
        f"<tr><th>strength</th><td>{_fmt(block['strength_score'], 2)}</td></tr>"
        f"<tr><th>touch count</th><td>{c['touch_count']}</td></tr>"
        f"<tr><th>touch volume</th><td>{_fmt(c['total_touch_volume'], 0)}</td></tr>"
        f"<tr><th>round-number bonus</th><td>{_fmt(c['round_number_bonus'], 2)}</td></tr>"
        f"<tr><th>hold direction</th><td>{html.escape(h['direction'])}</td></tr>"
        f"<tr><th>consecutive closes</th><td>{h['consecutive_bars']} / {h['required_bars']}</td></tr>"
        f"<tr><th>hold confirmed</th><td>{badge}</td></tr>"
        f"<tr><th>failed attempts</th><td>{h['failed_attempts']}</td></tr>"
        "</table>"
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
        return "<tr><td colspan='5' class='muted'>No closed trades yet.</td></tr>"
    rows = []
    for t in closed:
        cls = _sign_class(t["realized_pnl_pct"])
        pnl = "" if t["realized_pnl_pct"] is None else f"{_fmt(t['realized_pnl_pct'], 2)}%"
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(t['symbol']))}</td>"
            f"<td>{_fmt(t['entry_price'], 2)}</td><td>{_fmt(t['exit_price'], 2)}</td>"
            f"<td>{html.escape(str(t['exit_reason']))}</td>"
            f"<td class='{cls}'>{pnl}</td>"
            "</tr>"
        )
    return "".join(rows)


def _page(state: dict, journal: dict, poll_enabled: bool) -> str:
    sym = html.escape(str(state.get("symbol") or "—"))
    is_ok = state.get("status") == "ok"

    if is_ok:
        s = state["session"]
        price_cls = _cmp_class(state["last_price"], s["vwap"])
        ema9_cls = _cmp_class(state["last_price"], s["ema9"])
        hist_cls = _sign_class(s["macd"]["histogram"])
        price_val, bars_val = _fmt(state["last_price"], 2), str(state["bar_count"])
        extended_val = "yes" if state["last_bar_is_extended"] else "no"
        vwap_val, ema9_val, ema20_val = _fmt(s["vwap"]), _fmt(s["ema9"]), _fmt(s["ema20"])
        macd_val = _fmt(s["macd"]["macd"], 6)
        macd_sig_val = _fmt(s["macd"]["signal"], 6)
        macd_hist_val = _fmt(s["macd"]["histogram"], 6)
        relvol_val = _fmt(s["relative_volume"], 2)
        resistance_html = _level_block_html("Resistance (nearest above)", state["levels"]["resistance"])
        support_html = _level_block_html("Support (nearest below)", state["levels"]["support"])
        banner_text, banner_display = "", "display:none"
        main_display = ""
    else:
        price_cls = ema9_cls = hist_cls = ""
        price_val = bars_val = extended_val = "—"
        vwap_val = ema9_val = ema20_val = macd_val = macd_sig_val = macd_hist_val = relvol_val = "—"
        resistance_html = support_html = ""
        banner_text = (f"Warming up — waiting for bars for {sym}." if state.get("symbol")
                      else "No symbol selected yet — enter a ticker below.")
        banner_display, main_display = "", "display:none"

    journal_open_html = _journal_open_html(journal.get("open"))
    journal_closed_rows = _journal_closed_rows_html(journal.get("recent_closed") or [])

    body = f"""
<div id="banner" class="banner" style="{banner_display}">{html.escape(banner_text)}</div>
<div id="main" style="{main_display}">
  <section class="hero card">
    <div class="hero-symbol">{sym}</div>
    <div id="price-value" class="hero-price {price_cls}">{price_val}</div>
  </section>

  <section class="card">
    <h2>Virtual position</h2>
    <div id="journal-open">{journal_open_html}</div>
  </section>

  <section class="card">
    <h2>Indicators</h2>
    <table class="detail">
      <tr><th>Bars</th><td id="ind-bars">{bars_val}</td></tr>
      <tr><th>Last bar extended-hours</th><td id="ind-extended">{extended_val}</td></tr>
      <tr><th>VWAP (session)</th><td id="ind-vwap">{vwap_val}</td></tr>
      <tr><th>EMA 9</th><td id="ind-ema9" class="{ema9_cls}">{ema9_val}</td></tr>
      <tr><th>EMA 20</th><td id="ind-ema20">{ema20_val}</td></tr>
      <tr><th>MACD</th><td id="ind-macd">{macd_val}</td></tr>
      <tr><th>MACD signal</th><td id="ind-macd-signal">{macd_sig_val}</td></tr>
      <tr><th>MACD histogram</th><td id="ind-macd-hist" class="{hist_cls}">{macd_hist_val}</td></tr>
      <tr><th>Relative volume</th><td id="ind-relvol">{relvol_val}</td></tr>
    </table>
  </section>

  <section class="card">
    <h2>Levels</h2>
    <div id="resistance-block">{resistance_html}</div>
    <div id="support-block">{support_html}</div>
  </section>

  <section class="card">
    <h2>Recent closed trades</h2>
    <table class="detail">
      <tr><th>symbol</th><th>entry</th><th>exit</th><th>reason</th><th>P&amp;L %</th></tr>
      <tbody id="journal-closed-tbody">{journal_closed_rows}</tbody>
    </table>
  </section>
</div>
"""
    return _wrap(sym, body, poll_enabled)


_SYMBOL_FORM = (
    "<form method='post' action='/api/watch' class='ticker-form'>"
    "<input name='symbol' placeholder='Ticker' maxlength='10' autocomplete='off'>"
    "<button type='submit'>Watch</button>"
    "</form>"
)

# Live-polling toggle -- separate from the ticker form above (a page
# nobody's watching still burns a schwab-connector/API request every poll
# interval; this lets that stop without navigating away). poll_enabled
# reflects server-side truth (Poller.poll_enabled) at request time, same
# "real data on first paint" approach as the rest of _page -- the button
# doesn't just start on a guessed default and get corrected a moment later
# by JS.
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
function levelBlockHtml(title, block) {
  if (!block) {
    return '<h3>' + esc(title) + '</h3><p class="muted">none on this side of price</p>';
  }
  const h = block.hold, c = block.components;
  const badge = h.confirmed
    ? '<span class="badge badge-confirmed">confirmed</span>'
    : '<span class="badge badge-pending">not confirmed</span>';
  return '<h3>' + esc(title) + ' @ ' + fmt(block.price, 2) + '</h3>' +
    '<table class="detail">' +
    '<tr><th>strength</th><td>' + fmt(block.strength_score, 2) + '</td></tr>' +
    '<tr><th>touch count</th><td>' + c.touch_count + '</td></tr>' +
    '<tr><th>touch volume</th><td>' + fmt(c.total_touch_volume, 0) + '</td></tr>' +
    '<tr><th>round-number bonus</th><td>' + fmt(c.round_number_bonus, 2) + '</td></tr>' +
    '<tr><th>hold direction</th><td>' + esc(h.direction) + '</td></tr>' +
    '<tr><th>consecutive closes</th><td>' + h.consecutive_bars + ' / ' + h.required_bars + '</td></tr>' +
    '<tr><th>hold confirmed</th><td>' + badge + '</td></tr>' +
    '<tr><th>failed attempts</th><td>' + h.failed_attempts + '</td></tr>' +
    '</table>';
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
    return '<tr><td colspan="5" class="muted">No closed trades yet.</td></tr>';
  }
  return closed.map(function(t) {
    const cls = signClass(t.realized_pnl_pct);
    const pnl = t.realized_pnl_pct === null ? '' : fmt(t.realized_pnl_pct, 2) + '%';
    return '<tr><td>' + esc(t.symbol) + '</td><td>' + fmt(t.entry_price, 2) + '</td>' +
      '<td>' + fmt(t.exit_price, 2) + '</td><td>' + esc(t.exit_reason) + '</td>' +
      '<td class="' + cls + '">' + pnl + '</td></tr>';
  }).join('');
}
async function refresh() {
  let data;
  try {
    const r = await fetch('/api/state');
    data = await r.json();
  } catch (e) {
    return;  // keep showing the last-good render, same philosophy as the server's own _poll_ok
  }
  applyPollUiState(data.poll_enabled);  // stay in sync even if another tab paused/resumed it
  const banner = document.getElementById('banner');
  const main = document.getElementById('main');
  if (data.status !== 'ok') {
    banner.textContent = data.symbol
      ? ('Warming up \\u2014 waiting for bars for ' + data.symbol + '.')
      : 'No symbol selected yet \\u2014 enter a ticker below.';
    banner.style.display = '';
    main.style.display = 'none';
    return;
  }
  banner.style.display = 'none';
  main.style.display = '';

  const s = data.session;
  const priceEl = document.getElementById('price-value');
  priceEl.textContent = fmt(data.last_price, 2);
  priceEl.className = 'hero-price ' + cmpClass(data.last_price, s.vwap);
  document.getElementById('ind-bars').textContent = data.bar_count;
  document.getElementById('ind-extended').textContent = data.last_bar_is_extended ? 'yes' : 'no';
  document.getElementById('ind-vwap').textContent = fmt(s.vwap, 4);
  const ema9El = document.getElementById('ind-ema9');
  ema9El.textContent = fmt(s.ema9, 4);
  ema9El.className = cmpClass(data.last_price, s.ema9);
  document.getElementById('ind-ema20').textContent = fmt(s.ema20, 4);
  document.getElementById('ind-macd').textContent = fmt(s.macd.macd, 6);
  document.getElementById('ind-macd-signal').textContent = fmt(s.macd.signal, 6);
  const histEl = document.getElementById('ind-macd-hist');
  histEl.textContent = fmt(s.macd.histogram, 6);
  histEl.className = signClass(s.macd.histogram);
  document.getElementById('ind-relvol').textContent = fmt(s.relative_volume, 2);

  document.getElementById('resistance-block').innerHTML =
    levelBlockHtml('Resistance (nearest above)', data.levels.resistance);
  document.getElementById('support-block').innerHTML =
    levelBlockHtml('Support (nearest below)', data.levels.support);

  document.getElementById('journal-open').innerHTML = journalOpenHtml(data.journal.open);
  document.getElementById('journal-closed-tbody').innerHTML = journalClosedRows(data.journal.recent_closed);
}

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
  max-width:52rem;margin-inline:auto}
h1,h2,h3{margin:0 0 .5rem}
h2{font-size:.95rem;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
h3{font-size:.9rem;color:var(--text)}
.topbar{display:flex;align-items:center;justify-content:space-between;
  flex-wrap:wrap;gap:1rem;margin-bottom:1rem}
.ticker-form{display:flex;gap:.4rem}
.ticker-form input{text-transform:uppercase;background:var(--card);
  border:1px solid var(--border);color:var(--text);border-radius:.4rem;
  padding:.4rem .6rem;width:7rem}
.ticker-form button{background:var(--accent);color:#fff;border:none;
  border-radius:.4rem;padding:.4rem .8rem;cursor:pointer}
.poll-controls{display:flex;align-items:center;gap:.5rem;font-size:.85rem}
.poll-controls button{background:var(--card);color:var(--text);
  border:1px solid var(--border);border-radius:.4rem;padding:.4rem .8rem;
  cursor:pointer}
.banner{background:var(--pending);color:#1a1400;padding:.6rem 1rem;
  border-radius:.4rem;margin-bottom:1rem;font-weight:600}
.card{background:var(--card);border:1px solid var(--border);
  border-radius:.6rem;padding:1rem 1.2rem;margin-bottom:1rem}
.hero{display:flex;align-items:baseline;gap:1rem}
.hero-symbol{font-size:1.4rem;font-weight:700;letter-spacing:.02em}
.hero-price{font-size:2.4rem;font-weight:700;font-variant-numeric:tabular-nums}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
table.detail th,table.detail td{padding:.3rem .5rem;text-align:left;
  border-bottom:1px solid var(--border);font-size:.9rem}
table.detail th{color:var(--muted);font-weight:500;width:45%}
.pos{color:var(--pos);font-weight:600}
.neg{color:var(--neg);font-weight:600}
.muted{color:var(--muted)}
.badge{display:inline-block;padding:.1rem .5rem;border-radius:1rem;
  font-size:.78rem;font-weight:600}
.badge-confirmed{background:rgba(62,207,126,.18);color:var(--pos)}
.badge-pending{background:rgba(201,162,39,.18);color:var(--pending)}
.footer{color:var(--muted);font-size:.8rem;margin-top:1rem}
"""


def _wrap(sym: str, body: str, poll_enabled: bool) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>momentum monitor — {sym}</title>"
        f"<style>{_STYLE}</style></head><body>"
        f"<div class='topbar'><h1>{sym}</h1>{_SYMBOL_FORM}{_poll_toggle_html(poll_enabled)}</div>"
        f"{body}"
        "<p class='footer'>Read-only technical read. Not advice, not an order.</p>"
        f"<script>{_SCRIPT}</script>"
        "</body></html>"
    )


def create_app(*, fetch_bars, watch_symbol, poll_interval: float = 5.0,
               announce_watch=None, announce_unwatch=None,
               announce_retry_attempts=ANNOUNCE_RETRY_ATTEMPTS,
               announce_retry_base_delay=ANNOUNCE_RETRY_BASE_DELAY_SECONDS,
               announce_retry_max_delay=ANNOUNCE_RETRY_MAX_DELAY_SECONDS,
               journal_store=None, trail_pct=DEFAULT_TRAIL_PCT,
               now_fn=time.time) -> FastAPI:
    poller = Poller(fetch_bars=fetch_bars, watch_symbol=watch_symbol,
                    poll_interval=poll_interval, announce_watch=announce_watch,
                    announce_unwatch=announce_unwatch,
                    announce_retry_attempts=announce_retry_attempts,
                    announce_retry_base_delay=announce_retry_base_delay,
                    announce_retry_max_delay=announce_retry_max_delay,
                    journal_store=journal_store, trail_pct=trail_pct,
                    now_fn=now_fn)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        task = asyncio.create_task(poller.run())
        yield
        task.cancel()

    app = FastAPI(title="monitor-app", lifespan=lifespan)

    @app.get("/api/state")
    async def api_state():
        payload = dict(poller.state)
        payload["journal"] = poller.journal_snapshot()
        payload["poll_enabled"] = poller.poll_enabled
        return JSONResponse(payload)

    @app.get("/", response_class=HTMLResponse)
    async def root():
        return _page(poller.state, poller.journal_snapshot(), poller.poll_enabled)

    @app.post("/api/watch")
    async def api_watch(request: Request):
        # Parsed manually (not via FastAPI's Form(...)) so this endpoint
        # doesn't need the python-multipart dependency: a plain HTML form
        # POST (no file inputs) is application/x-www-form-urlencoded, which
        # Starlette/urllib can already parse without it.
        body = (await request.body()).decode()
        symbol = (parse_qs(body).get("symbol") or [""])[0]
        await poller.switch_symbol(symbol)
        return RedirectResponse("/", status_code=303)

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

    return app
