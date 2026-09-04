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
  REPLAY_PATH        fixture for STREAM_SOURCE=replay      (default /data/replay.jsonl)

Run:  uvicorn main:app --host 0.0.0.0 --port 8010
"""
from __future__ import annotations

import os

from app import create_app
from reconnect import ReconnectingStreamSource
from store import BarStore
from stream import ReplayStreamSource, SchwabStreamSource
from token_source import AccessTokenSource

STREAM_SOURCE = os.environ.get("STREAM_SOURCE", "schwab").lower()
BAR_DB_DIR = os.environ.get("BAR_DB_DIR", "/data/bars")
API_KEY = os.environ.get("SCHWAB_API_KEY", "")
APP_SECRET = os.environ.get("SCHWAB_APP_SECRET", "")
AUTH_HELPER_URL = os.environ.get("AUTH_HELPER_URL", "").strip()
REPLAY_PATH = os.environ.get("REPLAY_PATH", "/data/replay.jsonl")

_store = BarStore(BAR_DB_DIR)

if STREAM_SOURCE == "replay":
    def _source_factory():
        return ReplayStreamSource(REPLAY_PATH, pace=True)
    _replay = True
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
            token_source=AccessTokenSource(AUTH_HELPER_URL),
            build_client=_build_client,
            make_source=lambda client: SchwabStreamSource(client),
        )
    _replay = False

app = create_app(store=_store, source_factory=_source_factory, replay=_replay)
