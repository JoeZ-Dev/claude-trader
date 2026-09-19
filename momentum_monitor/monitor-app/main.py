"""
Production wiring for monitor-app. Binds create_app's fetch_bars /
announce_watch / announce_unwatch / stream_events to httpx calls against
schwab-connector, and journal_store to a real SQLite file (journal_store.py).

Environment:
  SCHWAB_CONNECTOR_URL  base URL of schwab-connector   (default http://schwab-connector:7878)
  WATCH_SYMBOL          the STARTING symbol only (phase 2: up to app.py's
                        MAX_SYMBOLS=4 can be watched concurrently; more are
                        added at runtime via POST /api/watch, not this env
                        var)                            (default: unset -> idle)
  JOURNAL_DB_PATH       SQLite file for the virtual trade journal
                        (specs.md section 6)              (default /data/journal.db)
  TRAIL_PCT             trailing-stop percent below the running high-water
                        mark for the virtual journal (specs.md section 6) --
                        only the SEED default for the strategy_params table
                        (section 8): read once, on the very first run
                        against a given journal.db, to initialize the
                        live-tunable value there. Every run after that
                        reads the DB, not this env var -- change the value
                        via POST /api/strategy_params, not by editing this
                        and redeploying               (default 0.05)
  VOLUME_CONFIRM_THRESHOLD  how far above "average" (1.0 = equal to the
                        trailing 20-bar volume average) a bar's volume must
                        be, at the moment a setup type confirms, for a
                        virtual entry to fire (specs.md section 6) -- same
                        seed-only treatment as TRAIL_PCT above, live-tuned
                        via the API from then on     (default 1.5)
  BASE_EQUITY           dollar "reset target" for the virtual account --
                        position sizing with compounding equity (specs.md
                        section 7) -- same seed-only treatment as TRAIL_PCT:
                        read once, on the very first run against a given
                        journal.db, to seed BOTH the live-tunable
                        strategy_params row AND current_equity's own
                        starting value. Every run after that reads the DB,
                        not this env var                (default 2000)
  RISK_PCT_PER_TRADE     fraction of current_equity risked on a single
                        entry (0.01 = 1%, same convention the EOD swing bot
                        used) -- same seed-only treatment  (default 0.01)
  SWING_LOW_BUFFER_PCT   cushion below whatever anchors phase 1's stop (a
                        confirmed swing low, or the entry-trigger level)
                        (specs.md section 12) -- same seed-only treatment
                        as TRAIL_PCT                     (default 0.005)
  PATTERN_PROGRESS_THRESHOLD_PCT  how far above entry (via high_water_mark)
                        price must climb before phase 1 hands off to
                        phase 2's flat trailing stop -- same seed-only
                        treatment                          (default 0.03)
  SESSION_VOLUME_MULTIPLE  how many multiples of a symbol's typical daily
                        volume today's cumulative session volume must
                        clear for an entry to fire (specs.md section 12,
                        stacking with VOLUME_CONFIRM_THRESHOLD above, not
                        replacing it) -- same seed-only treatment
                                                              (default 3.0)
  CONTINUATION_LOOKBACK_DAYS  how many recent trading days the
                        continuation-vs-fresh-day flag scans for a
                        qualifying move (specs.md section 7) --
                        INFORMATIONAL only, never gates an entry -- same
                        seed-only treatment                    (default 7)
  CONTINUATION_THRESHOLD_PCT  how large a single day's move (either
                        direction) must be to flag that day -- same
                        seed-only treatment                  (default 0.5)
  CONFIRMATION_FRESHNESS_SECONDS  how long a persisted confirmed=True
                        stays actionable for a NEW entry after it was
                        last genuinely reaffirmed (specs.md section 20)
                        -- same seed-only treatment            (default 30)
  TARGET_REFERENCE_PCT  entry_price * (1 + this) shown as an
                        informational reference alongside the real
                        trailing stop -- never an exit trigger (specs.md
                        section 21) -- same seed-only treatment
                                                              (default 0.10)
  MARKET_BACKDROP_SYMBOL  broad-market symbol whose day change is shown as
                        global, informational-only context (specs.md
                        section 23) -- never wired into should_enter/
                        advance_journal/sizing, and NOT a seed-only value
                        (no strategy_params row; it's a plain runtime
                        setting, not a live-tunable strategy threshold)
                                                              (default SPY)
  MARKET_BACKDROP_REFRESH_SECONDS  how often the market-backdrop poll
                        re-fetches -- its own independent periodic REST
                        poll, not tied to the 4 watched symbols' push
                        updates                              (default 180)

Run:  uvicorn main:app --host 0.0.0.0 --port 8012
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

import httpx

from app import create_app
from journal_store import JournalStore

logger = logging.getLogger("monitor-app.stream")

CONNECTOR_URL = os.environ.get("SCHWAB_CONNECTOR_URL", "http://schwab-connector:7878").rstrip("/")
WATCH_SYMBOL = os.environ.get("WATCH_SYMBOL") or None
JOURNAL_DB_PATH = os.environ.get("JOURNAL_DB_PATH", "/data/journal.db")
TRAIL_PCT = float(os.environ.get("TRAIL_PCT", "0.05"))
VOLUME_CONFIRM_THRESHOLD = float(os.environ.get("VOLUME_CONFIRM_THRESHOLD", "1.5"))
BASE_EQUITY = float(os.environ.get("BASE_EQUITY", "2000"))
RISK_PCT_PER_TRADE = float(os.environ.get("RISK_PCT_PER_TRADE", "0.01"))
SWING_LOW_BUFFER_PCT = float(os.environ.get("SWING_LOW_BUFFER_PCT", "0.005"))
PATTERN_PROGRESS_THRESHOLD_PCT = float(
    os.environ.get("PATTERN_PROGRESS_THRESHOLD_PCT", "0.03"))
SESSION_VOLUME_MULTIPLE = float(os.environ.get("SESSION_VOLUME_MULTIPLE", "3.0"))
CONTINUATION_LOOKBACK_DAYS = float(os.environ.get("CONTINUATION_LOOKBACK_DAYS", "7"))
CONTINUATION_THRESHOLD_PCT = float(os.environ.get("CONTINUATION_THRESHOLD_PCT", "0.5"))
CONFIRMATION_FRESHNESS_SECONDS = float(os.environ.get("CONFIRMATION_FRESHNESS_SECONDS", "30"))
TARGET_REFERENCE_PCT = float(os.environ.get("TARGET_REFERENCE_PCT", "0.10"))
MARKET_BACKDROP_SYMBOL = os.environ.get("MARKET_BACKDROP_SYMBOL", "SPY")
MARKET_BACKDROP_REFRESH_SECONDS = float(os.environ.get("MARKET_BACKDROP_REFRESH_SECONDS", "180"))

_client = httpx.AsyncClient(timeout=10.0)
_journal_store = JournalStore(JOURNAL_DB_PATH, default_params={
    "trail_pct": TRAIL_PCT,
    "volume_confirm_threshold": VOLUME_CONFIRM_THRESHOLD,
    "base_equity": BASE_EQUITY,
    "risk_pct_per_trade": RISK_PCT_PER_TRADE,
    "swing_low_buffer_pct": SWING_LOW_BUFFER_PCT,
    "pattern_progress_threshold_pct": PATTERN_PROGRESS_THRESHOLD_PCT,
    "session_volume_multiple": SESSION_VOLUME_MULTIPLE,
    "continuation_lookback_days": CONTINUATION_LOOKBACK_DAYS,
    "continuation_threshold_pct": CONTINUATION_THRESHOLD_PCT,
    "confirmation_freshness_seconds": CONFIRMATION_FRESHNESS_SECONDS,
    "target_reference_pct": TARGET_REFERENCE_PCT,
})


async def fetch_bars(symbol: str, since_ts: float):
    r = await _client.get(f"{CONNECTOR_URL}/bars/{symbol}",
                          params={"since_ts": since_ts})
    r.raise_for_status()
    return r.json()


async def announce_watch(symbol: str):
    r = await _client.post(f"{CONNECTOR_URL}/watch", json={"symbol": symbol})
    r.raise_for_status()


async def announce_unwatch(symbol: str):
    r = await _client.post(f"{CONNECTOR_URL}/unwatch", json={"symbol": symbol})
    r.raise_for_status()


async def fetch_daily_bars(symbol: str):
    # Session-level volume gate's "typical daily volume" baseline
    # (specs.md section 12) -- called ONCE per symbol, at add-time
    # (Poller.add_symbol), never per-bar. A non-2xx or network failure
    # propagates (add_symbol catches it, logs, and leaves avg_daily_
    # volume None -- skip the gate for that symbol, never a crash).
    r = await _client.get(f"{CONNECTOR_URL}/daily_bars/{symbol}")
    r.raise_for_status()
    return r.json().get("bars", [])


async def fetch_market_backdrop(symbol: str):
    # Market backdrop display (specs.md section 23) -- reuses this SAME
    # /daily_bars route (built for the volume gate above), just with
    # include_today=true so the still-forming current day's candle (whose
    # close is Schwab's live-updating current price) comes back alongside
    # the prior COMPLETED day's close. Called on its own periodic cycle
    # (Poller.run_market_backdrop_loop), never per-bar. A non-2xx or
    # network failure propagates -- refresh_market_backdrop catches it,
    # logs, and leaves the backdrop "unknown" rather than crashing the
    # loop or showing a stale value.
    r = await _client.get(f"{CONNECTOR_URL}/daily_bars/{symbol}",
                          params={"lookback_days": 2, "include_today": "true"})
    r.raise_for_status()
    return r.json().get("bars", [])


async def _consume_events_once(on_bar) -> None:
    async with _client.stream("GET", f"{CONNECTOR_URL}/events") as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue  # keep-alive/connected comment lines start with ':'
            payload = json.loads(line[len("data:"):].strip())
            await on_bar(payload["symbol"], payload["bar"])


async def stream_events(on_bar, on_reconnect, *, base_delay=1.0, max_delay=30.0) -> None:
    """Leg 1 of the poll -> push replacement (specs.md): one long-lived
    task consuming schwab-connector's single shared SSE connection
    (GET /events), covering every watched symbol. `on_reconnect` runs
    BEFORE every (re)connect's read loop, including the first, so a
    resync always closes the gap between what monitor-app has and "now"
    -- whether the connection wasn't up yet or just dropped mid-session.
    Purpose-built retry loop rather than reusing schwab-connector's own
    ReconnectingStreamSource -- that class's machinery (Schwab-token
    refresh, proactive-staleness deadline, auth-error handling) belongs
    to Schwab-auth state this internal leg doesn't have; "connect, read,
    backoff-and-retry on any break" is all this leg needs."""
    delay = base_delay
    while True:
        try:
            await on_reconnect()
            await _consume_events_once(on_bar)
            delay = base_delay
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("schwab-connector /events dropped, retrying in %.1fs: %s",
                           delay, exc)
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)


app = create_app(fetch_bars=fetch_bars, watch_symbol=WATCH_SYMBOL,
                 announce_watch=announce_watch, announce_unwatch=announce_unwatch,
                 stream_events=stream_events,
                 journal_store=_journal_store, trail_pct=TRAIL_PCT,
                 volume_confirm_threshold=VOLUME_CONFIRM_THRESHOLD,
                 base_equity=BASE_EQUITY, risk_pct_per_trade=RISK_PCT_PER_TRADE,
                 swing_low_buffer_pct=SWING_LOW_BUFFER_PCT,
                 pattern_progress_threshold_pct=PATTERN_PROGRESS_THRESHOLD_PCT,
                 session_volume_multiple=SESSION_VOLUME_MULTIPLE,
                 continuation_lookback_days=CONTINUATION_LOOKBACK_DAYS,
                 continuation_threshold_pct=CONTINUATION_THRESHOLD_PCT,
                 confirmation_freshness_seconds=CONFIRMATION_FRESHNESS_SECONDS,
                 target_reference_pct=TARGET_REFERENCE_PCT,
                 fetch_daily_bars=fetch_daily_bars,
                 fetch_market_backdrop=fetch_market_backdrop,
                 market_backdrop_symbol=MARKET_BACKDROP_SYMBOL,
                 market_backdrop_refresh_seconds=MARKET_BACKDROP_REFRESH_SECONDS)
