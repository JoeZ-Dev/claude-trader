"""
Reconnecting Schwab stream wrapper.

`companion-auth` vends access-token-only responses, so schwab-py cannot
self-refresh (see specs.md section 4). This wrapper keeps a live stream
going across the ~30-minute access-token lifetime by managing renewal
explicitly:

- Before each (re)connect it calls `token_source.refresh_async()` for a
  fresh access token (the async, thread-executor-backed variant of
  `refresh()` -- see token_source.py; calling the synchronous `refresh()`
  directly here would block this whole process's event loop for the
  HTTP call's duration on every single reconnect), then
  `build_client(token_source.as_schwab_token())` to make a new schwab-py
  client (via `client_from_access_functions`), then `make_source(client)`
  for a new inner stream source.
- It consumes ticks from the inner source until EITHER the token is within
  its leeway of expiry (`token_source.seconds_until_stale()` reaches 0 --
  the proactive path, so there is no dropped-tick window) OR the inner
  stream ends or errors (the reactive path).
- On either exit it loops: refresh, rebuild, reconnect. Reconnection is a
  routine ~30-minute event, not a failure case.
- `AuthRequired` / `AuthHelperError` from the token source are caught, not
  allowed to crash the consume task: it emits an event, sleeps
  `auth_retry_seconds`, and retries.
- If a fresh refresh_async() immediately produces an already-stale budget
  (zero ticks consumed before the proactive-refresh path fires), that
  same `auth_retry_seconds` backoff applies before looping back to
  refresh again. Confirmed live (2026-09-16): without this, a
  persistently-stale-token condition (e.g. the auth helper serving an
  already-near-expiry cached token) turns into a tight, zero-await
  busy-loop -- observed at ~13 reconnects/sec against the auth helper,
  sustained for a full hour, and severe enough to starve this process's
  own event loop (which stalled /health handling badly enough to fail
  the docker-compose healthcheck) since nothing in that path awaited
  anything.

Presents the same interface as the other stream sources: an async
`ticks(symbol)` generator and a `connected` property.

The `build_client` / `make_source` seams are injected so tests exercise the
orchestration without a real schwab-py client or network; main.py wires the
real ones.
"""
from __future__ import annotations

import asyncio
import time

from token_source import AuthHelperError, AuthRequired

DEFAULT_AUTH_RETRY_SECONDS = 60.0


class ReconnectingStreamSource:
    def __init__(self, *, token_source, build_client, make_source,
                 now_fn=time.time, sleep_fn=None,
                 auth_retry_seconds: float = DEFAULT_AUTH_RETRY_SECONDS,
                 on_event=None) -> None:
        self._token_source = token_source
        self._build_client = build_client
        self._make_source = make_source
        self._now = now_fn
        self._sleep = sleep_fn or asyncio.sleep
        self._auth_retry_seconds = auth_retry_seconds
        self._on_event = on_event or (lambda *a, **k: None)
        self._connected = False
        self._reconnect_count = 0

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def reconnect_count(self) -> int:
        return self._reconnect_count

    async def ticks(self, symbol: str):
        # symbol is threaded into every on_event() call below -- with up to
        # MAX_SYMBOLS independent ReconnectingStreamSource instances sharing
        # one process-wide `log_event` callback (main.py), a log line with
        # no symbol on it is ambiguous about which of the concurrent streams
        # it's even about. Found live (2026-09-17): a stream_error for one
        # of 3 newly-(re)watched symbols couldn't be pinned to a specific
        # symbol from the logs alone, slowing down a real diagnosis.
        first = True
        try:
            while True:
                # 1. fresh access token
                try:
                    await self._token_source.refresh_async()
                except (AuthRequired, AuthHelperError) as exc:
                    self._connected = False
                    self._on_event("auth_error", symbol=symbol, error=exc)
                    await self._sleep(self._auth_retry_seconds)
                    continue

                # 2. new schwab-py client + inner stream source
                try:
                    client = self._build_client(self._token_source.as_schwab_token())
                    inner = self._make_source(client)
                except Exception as exc:  # defensive: a bad client build
                    self._connected = False
                    self._on_event("build_error", symbol=symbol, error=exc)
                    await self._sleep(self._auth_retry_seconds)
                    continue

                if not first:
                    self._reconnect_count += 1
                    self._on_event("reconnect", symbol=symbol, count=self._reconnect_count)
                first = False

                # 3. consume until token near-expiry, or inner ends/errors
                got_a_tick = False
                async for tick in self._consume_until_stale(inner, symbol):
                    got_a_tick = True
                    yield tick
                self._connected = False
                if not got_a_tick:
                    # A fresh refresh_async() immediately produced an
                    # already-stale budget -- see module docstring. Back
                    # off instead of looping straight back into another
                    # refresh with no delay.
                    self._on_event("stale_immediately_after_refresh", symbol=symbol)
                    await self._sleep(self._auth_retry_seconds)
                # loop -> refresh + rebuild + reconnect
        finally:
            self._connected = False

    async def _consume_until_stale(self, inner, symbol: str):
        gen = inner.ticks(symbol).__aiter__()
        try:
            while True:
                budget = self._token_source.seconds_until_stale()
                if budget <= 0:
                    self._on_event("proactive_refresh", symbol=symbol)
                    return
                try:
                    tick = await asyncio.wait_for(gen.__anext__(), timeout=budget)
                except asyncio.TimeoutError:
                    # Hit the proactive-refresh deadline while waiting for the
                    # next tick (quiet market or slow stream).
                    self._on_event("proactive_refresh", symbol=symbol)
                    return
                except StopAsyncIteration:
                    self._on_event("stream_ended", symbol=symbol)
                    return
                except Exception as exc:
                    self._on_event("stream_error", symbol=symbol, error=exc)
                    return
                self._connected = getattr(inner, "connected", True)
                yield tick
        finally:
            aclose = getattr(gen, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:
                    pass
