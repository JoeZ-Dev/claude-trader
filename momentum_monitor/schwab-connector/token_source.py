"""
Access-token acquisition from the joelab `companion-auth` helper.

`schwab-connector` does not perform Schwab OAuth itself and never holds a
refresh token. It fetches short-lived access tokens from `companion-auth`
over the internal `joelab-ingress` network:

    GET {AUTH_HELPER_URL}/access_token
        X-Internal-Auth: <INTERNAL_AUTH_SECRET>
        200 -> {"access_token": str, "expires_at": <unix int>, "source": str}
        401 -> {"error": "UNAUTHORIZED"}                          (bad/missing shared secret)
        409 -> {"error": "AUTH_REQUIRED", "message": str, ...}    (not bootstrapped)

(Contract from `companion-auth`'s own `app.py`.) `X-Internal-Auth` is the
service's actual security boundary -- Cloudflare path-scoping is
defense-in-depth on top of it, not a substitute -- so every request carries
it, and a 401 is raised as a distinct, actionable error rather than falling
into the generic upstream-failure path.

Because the response carries no `refresh_token`, schwab-py's built-in
auto-refresh (authlib, which needs a `refresh_token` in the token dict it
was constructed with) cannot run here. Per specs.md section 4, renewal is
managed explicitly: the caller rebuilds the schwab-py client from a fresh
access token and reconnects the stream shortly BEFORE `stale_at()`
(proactively, ~5 min before the ~30-min expiry, matching schwab-py's
leeway=300), so there is no dropped-tick window.

This module only talks to the helper and does the expiry accounting; it
does not build the schwab-py client or manage the stream.
"""
from __future__ import annotations

import asyncio
import os
import time

LEEWAY_SECONDS = 300  # matches schwab-py's authlib leeway convention
INTERNAL_AUTH_HEADER = "X-Internal-Auth"


class AuthRequired(RuntimeError):
    """The helper has no usable token (HTTP 409). A human must run the
    bootstrap OAuth on the homelab; nothing schwab-connector does can fix
    this on its own."""


class AuthHelperError(RuntimeError):
    """The helper responded in a way we can't use (bad status or payload)."""


def _httpx_get(url: str, headers: dict | None = None):
    import httpx

    resp = httpx.get(url, headers=headers, timeout=10.0)
    try:
        payload = resp.json()
    except Exception:
        payload = None
    return resp.status_code, payload


class AccessTokenSource:
    def __init__(self, base_url: str, *, http_get=None, now_fn=time.time,
                 leeway_seconds: int = LEEWAY_SECONDS, shared_secret: str | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._http_get = http_get or _httpx_get
        self._now = now_fn
        self._leeway = leeway_seconds
        self._secret = shared_secret if shared_secret is not None else os.environ.get("INTERNAL_AUTH_SECRET", "")
        self._access_token: str | None = None
        self._expires_at: float = 0.0
        self._creation_ts: float = 0.0

    # -- fetching / caching ------------------------------------------------

    def current(self) -> str:
        """Cached access token; fetches or re-fetches if absent or stale."""
        if self._access_token is None or self.is_stale():
            self.refresh()
        return self._access_token  # type: ignore[return-value]

    async def refresh_async(self) -> str:
        """Same as refresh(), but safe to call from an asyncio event loop:
        runs the underlying synchronous HTTP call (httpx.get by default) in
        a thread executor instead of blocking the loop for its duration.

        Added specifically for ReconnectingStreamSource.ticks() (reconnect.py),
        which calls this on every reconnect. Confirmed live (2026-09-16):
        a reconnect loop that calls the synchronous refresh() directly can
        starve this whole process's event loop -- which explained an
        otherwise-puzzling docker-compose healthcheck failure during that
        incident, since /health (FastAPI, same process) shares the loop
        being starved."""
        return await asyncio.to_thread(self.refresh)

    def refresh(self) -> str:
        """Unconditionally fetch a fresh access token from the helper."""
        status, payload = self._http_get(
            f"{self._base}/access_token",
            headers={INTERNAL_AUTH_HEADER: self._secret},
        )
        if status == 401:
            raise AuthHelperError(
                "companion-auth rejected X-Internal-Auth (401) -- check "
                "INTERNAL_AUTH_SECRET matches companion-auth's .env")
        if status == 409:
            msg = (payload or {}).get("message") if isinstance(payload, dict) else None
            raise AuthRequired(msg or "companion-auth has no token; run its bootstrap OAuth")
        if status != 200:
            raise AuthHelperError(
                f"companion-auth GET /access_token -> {status}: {payload}")
        if not isinstance(payload, dict) or "access_token" not in payload:
            raise AuthHelperError(
                f"companion-auth response missing access_token: {payload}")
        self._access_token = payload["access_token"]
        self._expires_at = float(payload.get("expires_at") or 0.0)
        self._creation_ts = self._now()
        return self._access_token

    # -- expiry accounting ----------------------------------------------

    def is_stale(self) -> bool:
        """True once within `leeway_seconds` of expiry (or past it)."""
        return self._now() >= self._expires_at - self._leeway

    def stale_at(self) -> float:
        """Unix time at which the cached token becomes stale. The reconnect
        loop should rebuild the schwab-py client at or just before this."""
        return self._expires_at - self._leeway

    def seconds_until_stale(self) -> float:
        return max(0.0, self.stale_at() - self._now())

    # -- schwab-py adapter --------------------------------------------

    def as_schwab_token(self) -> dict:
        """The metadata-wrapped structure schwab-py's
        `client_from_access_functions` token_read_func must return. Verified
        against `schwab.auth.TokenMetadata.from_loaded_token`, which requires
        the `creation_timestamp` / `token` envelope. No `refresh_token` is
        included by design -- see module docstring."""
        if self._access_token is None:
            self.refresh()
        return {
            "creation_timestamp": int(self._creation_ts),
            "token": {
                "access_token": self._access_token,
                "token_type": "Bearer",
                "expires_at": int(self._expires_at),
            },
        }
