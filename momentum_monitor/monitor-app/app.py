"""
monitor-app FastAPI web app.

Holds no credentials. A background poller pulls new bars from
schwab-connector, keeps the running bar list, and recomputes the full
state (state.build_state) after every poll. Serves:

  GET /api/state  -> the state dict (JSON), verbatim
  GET /           -> a plain auto-refreshing HTML view of the same data

create_app() takes fetch_bars / announce_watch as callables so tests inject
fakes; main.py binds them to httpx calls against schwab-connector.
"""
from __future__ import annotations

import asyncio
import html
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from state import build_state


class Poller:
    def __init__(self, *, fetch_bars, watch_symbol, poll_interval, announce_watch):
        self._fetch_bars = fetch_bars
        self._symbol = watch_symbol.upper() if watch_symbol else None
        self._interval = poll_interval
        self._announce_watch = announce_watch
        self._bars: list[dict] = []
        self._last_ts: float = 0.0
        self._state: dict = {"status": "warming_up", "symbol": self._symbol,
                             "bar_count": 0}
        self._poll_ok = False

    @property
    def state(self) -> dict:
        return self._state

    async def run(self) -> None:
        if self._symbol is None:
            return
        if self._announce_watch is not None:
            try:
                await self._announce_watch(self._symbol)
            except Exception:
                pass
        while True:
            await self._poll_once()
            await asyncio.sleep(self._interval)

    async def _poll_once(self) -> None:
        try:
            incoming = await self._fetch_bars(self._symbol, self._last_ts)
            appended = False
            for bar in incoming:
                if not self._bars or bar["ts"] > self._bars[-1]["ts"]:
                    self._bars.append(bar)
                    appended = True
            if appended:
                self._last_ts = self._bars[-1]["ts"]
            self._state = build_state(self._bars, self._symbol)
            self._poll_ok = True
        except Exception:
            self._poll_ok = False  # keep serving the last good state


def _page(state: dict) -> str:
    sym = html.escape(str(state.get("symbol") or "—"))
    if state.get("status") != "ok":
        body = f"<p>Warming up — waiting for bars for <b>{sym}</b>.</p>"
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


def _wrap(sym: str, body: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta http-equiv=\"refresh\" content=\"5\">"
        f"<title>momentum monitor — {sym}</title>"
        "<style>body{font:14px system-ui,sans-serif;margin:2rem;max-width:40rem}"
        "table{border-collapse:collapse;margin:.5rem 0}"
        "th,td{border:1px solid #ccc;padding:.25rem .6rem;text-align:left}"
        "th{background:#f4f4f4}</style></head><body>"
        f"<h1>{sym}</h1>{body}"
        "<p style='color:#888'>Read-only technical read. Not advice, not an order.</p>"
        "</body></html>"
    )


def create_app(*, fetch_bars, watch_symbol, poll_interval: float = 5.0,
               announce_watch=None) -> FastAPI:
    poller = Poller(fetch_bars=fetch_bars, watch_symbol=watch_symbol,
                    poll_interval=poll_interval, announce_watch=announce_watch)

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

    return app
