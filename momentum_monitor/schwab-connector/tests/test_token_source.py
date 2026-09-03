import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from token_source import (
    AccessTokenSource,
    AuthHelperError,
    AuthRequired,
)

NOW = 1_800_000_000.0


class FakeHelper:
    """Stand-in for GET {base}/access_token. Queue (status, payload) tuples;
    falls back to a long-lived 200 when the queue is empty."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        if self.responses:
            return self.responses.pop(0)
        return 200, {"access_token": "DEFAULT", "expires_at": NOW + 3600, "source": "homelab"}


def _clock(start=NOW):
    box = {"t": start}
    return box, (lambda: box["t"])


def test_refresh_returns_token_and_records_stale_at():
    box, now = _clock()
    src = AccessTokenSource("http://companion-auth:8766",
                            http_get=FakeHelper((200, {"access_token": "abc",
                                                       "expires_at": NOW + 1800})),
                            now_fn=now)
    assert src.refresh() == "abc"
    assert src.current() == "abc"
    assert src.stale_at() == NOW + 1800 - 300


def test_current_caches_until_stale():
    box, now = _clock()
    helper = FakeHelper((200, {"access_token": "t1", "expires_at": NOW + 1800}))
    src = AccessTokenSource("http://companion-auth:8766", http_get=helper, now_fn=now)
    src.current()
    src.current()
    assert len(helper.calls) == 1


def test_current_refetches_once_within_leeway_of_expiry():
    box, now = _clock()
    helper = FakeHelper(
        (200, {"access_token": "t1", "expires_at": NOW + 1800}),
        (200, {"access_token": "t2", "expires_at": NOW + 3600}),
    )
    src = AccessTokenSource("http://companion-auth:8766", http_get=helper, now_fn=now)
    assert src.current() == "t1"
    box["t"] = NOW + 1800 - 299  # inside the 300s leeway
    assert src.current() == "t2"
    assert len(helper.calls) == 2


def test_is_stale_boundary_is_inclusive():
    box, now = _clock()
    src = AccessTokenSource("http://x", http_get=FakeHelper(
        (200, {"access_token": "t", "expires_at": NOW + 300})), now_fn=now)
    src.refresh()
    assert src.is_stale() is True          # now == expires_at - leeway
    box["t"] = NOW - 1
    assert src.is_stale() is False


def test_seconds_until_stale_never_negative():
    box, now = _clock()
    src = AccessTokenSource("http://x", http_get=FakeHelper(
        (200, {"access_token": "t", "expires_at": NOW + 100})), now_fn=now)
    src.refresh()
    assert src.seconds_until_stale() == 0.0  # already within leeway


def test_409_raises_auth_required_with_message():
    src = AccessTokenSource("http://x", http_get=FakeHelper(
        (409, {"error": "AUTH_REQUIRED", "message": "Run bootstrap.py"})))
    with pytest.raises(AuthRequired, match="bootstrap"):
        src.refresh()


def test_unexpected_status_raises_auth_helper_error():
    src = AccessTokenSource("http://x", http_get=FakeHelper((503, {"msg": "down"})))
    with pytest.raises(AuthHelperError, match="503"):
        src.refresh()


def test_missing_access_token_field_raises():
    src = AccessTokenSource("http://x", http_get=FakeHelper((200, {"expires_at": NOW})))
    with pytest.raises(AuthHelperError, match="access_token"):
        src.refresh()


def test_base_url_trailing_slash_stripped():
    helper = FakeHelper((200, {"access_token": "t", "expires_at": NOW + 1800}))
    AccessTokenSource("http://companion-auth:8766/", http_get=helper,
                      now_fn=lambda: NOW).refresh()
    assert helper.calls == ["http://companion-auth:8766/access_token"]


def test_as_schwab_token_has_wrapper_shape_and_no_refresh_token():
    src = AccessTokenSource("http://x", http_get=FakeHelper(
        (200, {"access_token": "abc", "expires_at": NOW + 1800})), now_fn=lambda: NOW)
    wrapped = src.as_schwab_token()
    assert set(wrapped) == {"creation_timestamp", "token"}
    assert isinstance(wrapped["creation_timestamp"], int)
    assert wrapped["token"]["access_token"] == "abc"
    assert wrapped["token"]["token_type"] == "Bearer"
    assert wrapped["token"]["expires_at"] == NOW + 1800
    assert "refresh_token" not in wrapped["token"]


def test_as_schwab_token_fetches_when_empty():
    helper = FakeHelper((200, {"access_token": "abc", "expires_at": NOW + 1800}))
    src = AccessTokenSource("http://x", http_get=helper, now_fn=lambda: NOW)
    src.as_schwab_token()
    assert len(helper.calls) == 1
