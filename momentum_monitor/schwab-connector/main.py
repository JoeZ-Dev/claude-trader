"""
Production wiring for schwab-connector. `create_app` (in app.py) knows
nothing about Schwab or the filesystem; this module supplies both.

Environment:
  STREAM_SOURCE      "schwab" (default) or "replay"
  BAR_DB_DIR         per-symbol JSONL bar files            (default /data/bars)
  SCHWAB_API_KEY     Schwab app key
  SCHWAB_APP_SECRET  Schwab app secret
  AUTH_HELPER_URL    joelab companion-auth base URL, reached by service name on
                     joelab-ingress, e.g. http://companion-auth:<port>
                     (required for live mode; unset -> connector boots but
                     reports connected=false)
  INTERNAL_AUTH_SECRET  shared secret sent as the X-Internal-Auth header on
                     every companion-auth request; must match the value in
                     companion-auth's own .env (unset -> companion-auth
                     rejects every request with 401)
  REPLAY_PATH        fixture for STREAM_SOURCE=replay      (default /data/replay.jsonl)

Run:  uvicorn main:app --host 0.0.0.0 --port 7878
(internal only -- never published to the host; reached by service name on
the docker networks schwab-connector joins.)
"""
from __future__ import annotations

import logging
import os

from app import create_app
from events import log_event
from price_history import fetch_today_bars
from reconnect import ReconnectingStreamSource
from store import BarStore
from stream import ReplayStreamSource, SchwabStreamSource
from token_source import AccessTokenSource

logging.basicConfig(level=logging.INFO)

STREAM_SOURCE = os.environ.get("STREAM_SOURCE", "schwab").lower()
BAR_DB_DIR = os.environ.get("BAR_DB_DIR", "/data/bars")
API_KEY = os.environ.get("SCHWAB_API_KEY", "")
APP_SECRET = os.environ.get("SCHWAB_APP_SECRET", "")
AUTH_HELPER_URL = os.environ.get("AUTH_HELPER_URL", "").strip()
INTERNAL_AUTH_SECRET = os.environ.get("INTERNAL_AUTH_SECRET", "")
REPLAY_PATH = os.environ.get("REPLAY_PATH", "/data/replay.jsonl")

_store = BarStore(BAR_DB_DIR)

if STREAM_SOURCE == "replay":
    def _source_factory():
        return ReplayStreamSource(REPLAY_PATH, pace=True)
    _replay = True
    _history_fetcher = None  # replay fixtures are hand-crafted; no session to backfill
else:
    def _build_client(schwab_token: dict):
        # companion-auth vends access-token-only responses, so schwab-py's own
        # refresh cannot run; ReconnectingStreamSource rebuilds this client from
        # a fresh token before each expiry. See specs.md section 4.
        from schwab.auth import client_from_access_functions

        return client_from_access_functions(
            API_KEY, APP_SECRET,
            token_read_func=lambda: schwab_token,
            token_write_func=lambda *_a, **_k: None,
            asyncio=True,
        )

    def _source_factory():
        if not AUTH_HELPER_URL:
            raise RuntimeError(
                "AUTH_HELPER_URL is not set; cannot reach companion-auth for a token")
        return ReconnectingStreamSource(
            token_source=AccessTokenSource(AUTH_HELPER_URL, shared_secret=INTERNAL_AUTH_SECRET),
            build_client=_build_client,
            make_source=lambda client: SchwabStreamSource(client),
            on_event=log_event,
        )
    _replay = False

    async def _history_fetcher(symbol: str):
        # One-shot: a fresh token + client, used once for the price-history
        # call and discarded. Unlike the streaming path (ReconnectingStreamSource),
        # this doesn't need to survive 30 minutes, so it doesn't need the
        # reconnect machinery -- just a valid token at call time. See
        # price_history.py for why this backfill exists at all.
        if not AUTH_HELPER_URL:
            raise RuntimeError(
                "AUTH_HELPER_URL is not set; cannot reach companion-auth for a token")
        token_source = AccessTokenSource(AUTH_HELPER_URL, shared_secret=INTERNAL_AUTH_SECRET)
        token_source.refresh()
        client = _build_client(token_source.as_schwab_token())
        return await fetch_today_bars(client, symbol)

app = create_app(store=_store, source_factory=_source_factory, replay=_replay,
                 history_fetcher=_history_fetcher)
