import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from events import format_event, log_event

LOGGER_NAME = "schwab-connector.stream"


def test_format_event_with_no_kwargs():
    assert format_event("stream_ended") == "event=stream_ended"


def test_format_event_includes_kwargs():
    assert format_event("reconnect", count=3) == "event=reconnect count=3"


def test_format_event_renders_error_kwarg():
    exc = RuntimeError("boom")
    assert format_event("auth_error", error=exc) == f"event=auth_error error={exc!r}"


def test_log_event_emits_reconnect_at_info_level(caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        log_event("reconnect", count=1)
    assert "event=reconnect count=1" in caplog.text
    assert caplog.records[0].levelno == logging.INFO


def test_log_event_emits_auth_error_at_warning_level(caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        log_event("auth_error", error=RuntimeError("run bootstrap"))
    assert "event=auth_error" in caplog.text
    assert caplog.records[0].levelno == logging.WARNING


def test_log_event_emits_proactive_refresh_at_info_level(caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        log_event("proactive_refresh")
    assert "event=proactive_refresh" in caplog.text
    assert caplog.records[0].levelno == logging.INFO
