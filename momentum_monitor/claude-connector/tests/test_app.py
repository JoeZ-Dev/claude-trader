import os
import sys

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _APP_DIR)

from fastapi.testclient import TestClient

from app import create_app
from claude_cli import ClaudeInvocationError

_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
_SUCCESS = os.path.join(_FIXTURES, "fake_claude_success.sh")
_FAILURE = os.path.join(_FIXTURES, "fake_claude_failure.sh")
_SLOW = os.path.join(_FIXTURES, "fake_claude_slow.sh")


def test_narrate_returns_the_real_text_on_success():
    app = create_app(binary=_SUCCESS)
    with TestClient(app) as c:
        r = c.post("/narrate", json={"prompt": "hello"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["text"] == "FAKE_CLAUDE_OK: hello"


def test_narrate_returns_502_and_a_clear_error_on_invocation_failure():
    app = create_app(binary=_FAILURE)
    with TestClient(app) as c:
        r = c.post("/narrate", json={"prompt": "hello"})
        assert r.status_code == 502
        body = r.json()
        assert body["ok"] is False
        assert "authentication failed" in body["error"]


def test_narrate_returns_502_on_timeout_not_a_hang_or_500(tmp_path):
    app = create_app(binary=_SLOW, timeout_seconds=0.3)
    with TestClient(app) as c:
        r = c.post("/narrate", json={"prompt": "hello"})
        assert r.status_code == 502
        assert "timed out" in r.json()["error"].lower()


def test_narrate_never_raises_an_unhandled_exception_to_the_client():
    # A completely broken invocation path (binary genuinely missing) must
    # still come back as a clean, parseable JSON error -- never a bare
    # 500/stack trace leaking to the caller.
    app = create_app(binary="/no/such/binary")
    with TestClient(app) as c:
        r = c.post("/narrate", json={"prompt": "hello"})
        assert r.status_code == 502
        assert r.json()["ok"] is False


def test_health_endpoint_reports_ok():
    app = create_app(binary=_SUCCESS)
    with TestClient(app) as c:
        r = c.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
