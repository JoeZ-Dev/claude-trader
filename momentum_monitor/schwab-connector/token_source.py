"""
Access-token acquisition from the joelab `companion-auth` helper.

`schwab-connector` does not perform Schwab OAuth itself and never holds a
refresh token. It fetches short-lived access tokens from `companion-auth`
over the internal `joelab-ingress` network:

    GET {AUTH_HELPER_URL}/access_token
        200 -> {"access_token": str, "expires_at": <unix int>, "source": str}
        409 -> {"error": "AUTH_REQUIRED", "message": str, ...}   (not bootstrapped)

(Contract from `tools/auth_helper/server.py` in the ToS_Companion repo.)

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

import time

LEEWAY_SECONDS = 300  # matches schwab-py's authlib leeway convention


class AuthRequired(RuntimeError):
    """The helper has no usable token (HTTP 409). A human must run the
    bootstrap OAuth on the homelab; nothing schwab-connector does can fix
    this on its own."""


class AuthHelperError(RuntimeError):
    """The helper responded in a way we can't use (bad status or payload)."""


def _httpx_get(url: str):
    import httpx

    resp = httpx.get(url, timeout=10.0)
    try:
        payload = resp.json()
    except Exception:
        payload = None
    return resp.status_code, payload


class AccessTokenSource:
    def __init__(self, base_url: str, *, http_get=None, now_fn=time.time,
                 leeway_seconds: int = LEEWAY_SECONDS) -> None:
        self._base = base_url.rstrip("/")
        self._http_get = http_get or _httpx_get
        self._now = now_fn
        self._leeway = leeway_seconds
        self._access_token: str | None = None
        self._expires_at: float = 0.0
        self._creation_ts: float = 0.0

    # -- fetching / caching ------------------------------------------------

    def current(self) -> str:
        """Cached access token; fetches or re-fetches if absent or stale."""
        if self._access_token is None or self.is_stale():
            self.refresh()
        return self._access_token  # type: ignore[return-value]

    def refresh(self) -> str:
        """Unconditionally fetch a fresh access token from the helper."""
        status, payload = self._http_get(f"{self._base}/access_token")
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
