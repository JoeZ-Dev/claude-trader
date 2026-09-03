"""
Production wiring for schwab-connector. `create_app` (in app.py) holds no
knowledge of Schwab or the filesystem layout; this module supplies both.

Environment:
  STREAM_SOURCE      "schwab" (default) or "replay"
  BAR_DB_DIR         where per-symbol JSONL bar files live   (default /data/bars)
  SCHWAB_TOKEN_PATH  schwab-py token file                    (default /data/token.json)
  SCHWAB_API_KEY     Schwab app key   (Market Data Production app only)
  SCHWAB_APP_SECRET  Schwab app secret
  REPLAY_PATH        fixture for STREAM_SOURCE=replay        (default /data/replay.jsonl)

Run:  uvicorn main:app --host 0.0.0.0 --port 8010
"""
from __future__ import annotations

import os

from app import create_app
from store import BarStore
from stream import ReplayStreamSource, SchwabStreamSource

STREAM_SOURCE = os.environ.get("STREAM_SOURCE", "schwab").lower()
BAR_DB_DIR = os.environ.get("BAR_DB_DIR", "/data/bars")
TOKEN_PATH = os.environ.get("SCHWAB_TOKEN_PATH", "/data/token.json")
API_KEY = os.environ.get("SCHWAB_API_KEY", "")
APP_SECRET = os.environ.get("SCHWAB_APP_SECRET", "")
REPLAY_PATH = os.environ.get("REPLAY_PATH", "/data/replay.jsonl")

_store = BarStore(BAR_DB_DIR)

if STREAM_SOURCE == "replay":
    def _source_factory():
        return ReplayStreamSource(REPLAY_PATH, pace=True)
    _replay = True
else:
    def _source_factory():
        from schwab.auth import client_from_token_file

        client = client_from_token_file(
            TOKEN_PATH, API_KEY, APP_SECRET, asyncio=True
        )
        return SchwabStreamSource(client)
    _replay = False

app = create_app(store=_store, source_factory=_source_factory, replay=_replay)
