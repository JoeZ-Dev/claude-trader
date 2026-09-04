"""
Formats `ReconnectingStreamSource.on_event` callbacks (reconnect.py) into log
lines, so the routine ~30-minute reconnect cycle -- and any auth failures --
are visible via `docker compose logs schwab-connector` instead of being
silently dropped by the default no-op callback.

`format_event` is the pure, tested piece; `log_event` (the callable wired
into `main.py` as `on_event=log_event`) adds only the logging call itself.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("schwab-connector.stream")

_WARNING_EVENTS = {"auth_error", "build_error", "stream_error"}


def format_event(name: str, **kwargs) -> str:
    """Render one on_event(name, **kwargs) call as a single log line."""
    if not kwargs:
        return f"event={name}"
    parts = " ".join(f"{k}={v!r}" for k, v in kwargs.items())
    return f"event={name} {parts}"


def log_event(name: str, **kwargs) -> None:
    level = logging.WARNING if name in _WARNING_EVENTS else logging.INFO
    logger.log(level, format_event(name, **kwargs))
