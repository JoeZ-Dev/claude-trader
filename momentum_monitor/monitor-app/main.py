"""
Production wiring for monitor-app. Binds create_app's fetch_bars /
announce_watch / announce_unwatch to httpx calls against schwab-connector,
and journal_store to a real SQLite file (journal_store.py).

Environment:
  SCHWAB_CONNECTOR_URL  base URL of schwab-connector   (default http://schwab-connector:7878)
  WATCH_SYMBOL          the STARTING symbol only (phase 2: up to app.py's
                        MAX_SYMBOLS=4 can be watched concurrently; more are
                        added at runtime via POST /api/watch, not this env
                        var)                            (default: unset -> idle)
  POLL_INTERVAL         seconds between bar polls, per watched symbol (default 5)
  JOURNAL_DB_PATH       SQLite file for the virtual trade journal
                        (specs.md section 6)              (default /data/journal.db)
  TRAIL_PCT             trailing-stop percent below the running high-water
                        mark for the virtual journal -- a starting point to
                        tune against real logged data, NOT a validated
                        number (specs.md section 6)        (default 0.05)

Run:  uvicorn main:app --host 0.0.0.0 --port 8012
"""
from __future__ import annotations

import os

import httpx

from app import create_app
from journal_store import JournalStore

CONNECTOR_URL = os.environ.get("SCHWAB_CONNECTOR_URL", "http://schwab-connector:7878").rstrip("/")
WATCH_SYMBOL = os.environ.get("WATCH_SYMBOL") or None
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "5"))
JOURNAL_DB_PATH = os.environ.get("JOURNAL_DB_PATH", "/data/journal.db")
TRAIL_PCT = float(os.environ.get("TRAIL_PCT", "0.05"))

_client = httpx.AsyncClient(timeout=10.0)
_journal_store = JournalStore(JOURNAL_DB_PATH)


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


app = create_app(fetch_bars=fetch_bars, watch_symbol=WATCH_SYMBOL,
                 poll_interval=POLL_INTERVAL, announce_watch=announce_watch,
                 announce_unwatch=announce_unwatch,
                 journal_store=_journal_store, trail_pct=TRAIL_PCT)
