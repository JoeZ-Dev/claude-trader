"""
Production wiring for monitor-app. Binds create_app's fetch_bars /
announce_watch to httpx calls against schwab-connector.

Environment:
  SCHWAB_CONNECTOR_URL  base URL of schwab-connector   (default http://schwab-connector:8010)
  WATCH_SYMBOL          the one symbol to watch this phase (default: unset -> idle)
  POLL_INTERVAL         seconds between bar polls       (default 5)

Run:  uvicorn main:app --host 0.0.0.0 --port 8012
"""
from __future__ import annotations

import os

import httpx

from app import create_app

CONNECTOR_URL = os.environ.get("SCHWAB_CONNECTOR_URL", "http://schwab-connector:8010").rstrip("/")
WATCH_SYMBOL = os.environ.get("WATCH_SYMBOL") or None
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "5"))

_client = httpx.AsyncClient(timeout=10.0)


async def fetch_bars(symbol: str, since_ts: float):
    r = await _client.get(f"{CONNECTOR_URL}/bars/{symbol}",
                          params={"since_ts": since_ts})
    r.raise_for_status()
    return r.json()


async def announce_watch(symbol: str):
    r = await _client.post(f"{CONNECTOR_URL}/watch", json={"symbol": symbol})
    r.raise_for_status()


app = create_app(fetch_bars=fetch_bars, watch_symbol=WATCH_SYMBOL,
                 poll_interval=POLL_INTERVAL, announce_watch=announce_watch)
