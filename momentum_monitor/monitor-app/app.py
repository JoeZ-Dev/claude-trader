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

    @property
    def state(self) -> dict:
        return self._state

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
            if self._symbol is not None:
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


def _journal_html(journal: dict) -> str:
    open_block = journal.get("open")
    if open_block is not None:
        open_html = (
            "<table>"
            f"<tr><th>symbol</th><td>{html.escape(str(open_block['symbol']))}</td></tr>"
            f"<tr><th>entry price</th><td>{open_block['entry_price']}</td></tr>"
            f"<tr><th>trailing stop</th><td>{open_block['stop_level']}</td></tr>"
            f"<tr><th>unrealized P&amp;L %</th><td>{open_block['unrealized_pnl_pct']}</td></tr>"
            "</table>"
        )
    else:
        open_html = "<p>No open virtual position.</p>"

    closed = journal.get("recent_closed") or []
    if closed:
        rows = "".join(
            "<tr>"
            f"<td>{html.escape(str(t['symbol']))}</td>"
            f"<td>{t['entry_price']}</td><td>{t['exit_price']}</td>"
            f"<td>{html.escape(str(t['exit_reason']))}</td>"
            f"<td>{round(t['realized_pnl_pct'], 4) if t['realized_pnl_pct'] is not None else ''}</td>"
            "</tr>"
            for t in closed
        )
        closed_html = (
            "<table><tr><th>symbol</th><th>entry</th><th>exit</th>"
            "<th>reason</th><th>P&amp;L %</th></tr>" + rows + "</table>"
        )
    else:
        closed_html = "<p>No closed trades yet.</p>"

    return (
        "<h2>Virtual position</h2>" + open_html
        + "<h2>Recent closed trades</h2>" + closed_html
    )


def _page(state: dict, journal: dict) -> str:
    journal_body = _journal_html(journal)
    sym = html.escape(str(state.get("symbol") or "—"))
    if state.get("status") != "ok":
        if state.get("symbol"):
            body = f"<p>Warming up — waiting for bars for <b>{sym}</b>.</p>"
        else:
            body = "<p>No symbol selected yet — enter a ticker below.</p>"
        return _wrap(sym, body + journal_body)

    s = state["session"]
    rows = [
        ("Last price", state["last_price"]),
        ("Bars", state["bar_count"]),
        ("Last bar extended-hours", state["last_bar_is_extended"]),
        ("VWAP (session)", s["vwap"]),
        ("EMA 9", s["ema9"]),
        ("EMA 20", s["ema20"]),
        ("MACD", s["macd"]["macd"]),
        ("MACD signal", s["macd"]["signal"]),
        ("MACD histogram", s["macd"]["histogram"]),
        ("Relative volume", s["relative_volume"]),
    ]
    table = "".join(
        f"<tr><th>{html.escape(k)}</th><td>{html.escape(str(v))}</td></tr>"
        for k, v in rows
    )

    def level_html(title, block):
        if block is None:
            return f"<h2>{title}</h2><p>none on this side of price</p>"
        c = block["components"]
        h = block["hold"]
        return (
            f"<h2>{title} @ {block['price']}</h2>"
            f"<table>"
            f"<tr><th>strength</th><td>{block['strength_score']}</td></tr>"
            f"<tr><th>touch count</th><td>{c['touch_count']}</td></tr>"
            f"<tr><th>touch volume</th><td>{c['total_touch_volume']}</td></tr>"
            f"<tr><th>round-number bonus</th><td>{c['round_number_bonus']}</td></tr>"
            f"<tr><th>hold direction</th><td>{h['direction']}</td></tr>"
            f"<tr><th>consecutive closes</th><td>{h['consecutive_bars']} / {h['required_bars']}</td></tr>"
            f"<tr><th>hold confirmed</th><td>{h['confirmed']}</td></tr>"
            f"<tr><th>failed attempts</th><td>{h['failed_attempts']}</td></tr>"
            f"</table>"
        )

    body = (
        f"<table>{table}</table>"
        + level_html("Resistance (nearest above)", state["levels"]["resistance"])
        + level_html("Support (nearest below)", state["levels"]["support"])
        + journal_body
    )
    return _wrap(sym, body)


_SYMBOL_FORM = (
    "<form method='post' action='/api/watch' style='margin:.5rem 0'>"
    "<input name='symbol' placeholder='Ticker' maxlength='10' autocomplete='off' "
    "style='text-transform:uppercase'>"
    "<button type='submit'>Watch</button>"
    "</form>"
)


def _wrap(sym: str, body: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta http-equiv=\"refresh\" content=\"5\">"
        f"<title>momentum monitor — {sym}</title>"
        "<style>body{font:14px system-ui,sans-serif;margin:2rem;max-width:40rem}"
        "table{border-collapse:collapse;margin:.5rem 0}"
        "th,td{border:1px solid #ccc;padding:.25rem .6rem;text-align:left}"
        "th{background:#f4f4f4}</style></head><body>"
        f"<h1>{sym}</h1>{_SYMBOL_FORM}{body}"
        "<p style='color:#888'>Read-only technical read. Not advice, not an order.</p>"
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
        return JSONResponse(payload)

    @app.get("/", response_class=HTMLResponse)
    async def root():
        return _page(poller.state, poller.journal_snapshot())

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

    return app
