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
                      Poller.switch_symbol. Redirects back to / (303).

create_app() takes fetch_bars / announce_watch as callables so tests inject
fakes; main.py binds them to httpx calls against schwab-connector.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
from contextlib import asynccontextmanager
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from state import build_state

logger = logging.getLogger(__name__)

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
                 announce_retry_attempts=ANNOUNCE_RETRY_ATTEMPTS,
                 announce_retry_base_delay=ANNOUNCE_RETRY_BASE_DELAY_SECONDS,
                 announce_retry_max_delay=ANNOUNCE_RETRY_MAX_DELAY_SECONDS):
        self._fetch_bars = fetch_bars
        self._initial_symbol = watch_symbol.upper() if watch_symbol else None
        self._interval = poll_interval
        self._announce_watch = announce_watch
        self._announce_retry_attempts = announce_retry_attempts
        self._announce_retry_base_delay = announce_retry_base_delay
        self._announce_retry_max_delay = announce_retry_max_delay
        self._symbol: str | None = None
        self._bars: list[dict] = []
        self._last_ts: float = 0.0
        self._state: dict = build_state([], None)
        self._poll_ok = False

    @property
    def state(self) -> dict:
        return self._state

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
        watch always has, not a second-class cold start).

        A blank/invalid symbol, or the symbol already being watched, is a
        silent no-op -- returns False. Returns True if it actually switched."""
        new_symbol = new_symbol.strip().upper()
        if not new_symbol or new_symbol == self._symbol or not _VALID_SYMBOL.match(new_symbol):
            return False
        self._symbol = new_symbol
        self._bars = []
        self._last_ts = 0.0
        self._poll_ok = False
        self._state = build_state([], new_symbol)
        if self._announce_watch is not None:
            await self._announce_watch_with_retry()
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
        appended = False
        for bar in incoming:
            if not self._bars or bar["ts"] > self._bars[-1]["ts"]:
                self._bars.append(bar)
                appended = True
        if appended:
            self._last_ts = self._bars[-1]["ts"]
        self._state = build_state(self._bars, symbol)
        self._poll_ok = True


def _page(state: dict) -> str:
    sym = html.escape(str(state.get("symbol") or "—"))
    if state.get("status") != "ok":
        if state.get("symbol"):
            body = f"<p>Warming up — waiting for bars for <b>{sym}</b>.</p>"
        else:
            body = "<p>No symbol selected yet — enter a ticker below.</p>"
        return _wrap(sym, body)

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
               announce_watch=None,
               announce_retry_attempts=ANNOUNCE_RETRY_ATTEMPTS,
               announce_retry_base_delay=ANNOUNCE_RETRY_BASE_DELAY_SECONDS,
               announce_retry_max_delay=ANNOUNCE_RETRY_MAX_DELAY_SECONDS) -> FastAPI:
    poller = Poller(fetch_bars=fetch_bars, watch_symbol=watch_symbol,
                    poll_interval=poll_interval, announce_watch=announce_watch,
                    announce_retry_attempts=announce_retry_attempts,
                    announce_retry_base_delay=announce_retry_base_delay,
                    announce_retry_max_delay=announce_retry_max_delay)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        task = asyncio.create_task(poller.run())
        yield
        task.cancel()

    app = FastAPI(title="monitor-app", lifespan=lifespan)

    @app.get("/api/state")
    async def api_state():
        return JSONResponse(poller.state)

    @app.get("/", response_class=HTMLResponse)
    async def root():
        return _page(poller.state)

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
