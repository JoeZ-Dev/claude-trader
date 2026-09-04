import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from reconnect import ReconnectingStreamSource
from token_source import AccessTokenSource

NOW = 1_800_000_000.0


# --- fakes -----------------------------------------------------------------

class FakeHelper:
    """GET {base}/access_token stand-in. Queue (status, payload); default is a
    long-lived token."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, url):
        self.calls += 1
        if self.responses:
            return self.responses.pop(0)
        return 200, {"access_token": f"tok{self.calls}", "expires_at": NOW + 3600}


class FakeInner:
    """Fake stream source. `script` is a list of:
        ("tick", {...})      -> yield a tick
        ("raise", exc)       -> raise
        ("end",)             -> stop (StopAsyncIteration)
        ("hang", seconds)    -> await sleep(seconds) before continuing
    """

    def __init__(self, script):
        self._script = script
        self.connected = False
        self.ticks_calls = 0
        self.symbol = None

    async def ticks(self, symbol):
        self.ticks_calls += 1
        self.symbol = symbol
        self.connected = True
        for item in self._script:
            kind = item[0]
            if kind == "tick":
                yield item[1]
            elif kind == "raise":
                raise item[1]
            elif kind == "end":
                return
            elif kind == "hang":
                await asyncio.sleep(item[1])


class Harness:
    def __init__(self, helper_responses=(), inners=None, build_errors=None):
        self.helper = FakeHelper(*helper_responses)
        self.token_source = AccessTokenSource("http://companion-auth:9999",
                                              http_get=self.helper,
                                              now_fn=lambda: NOW)
        self._inners = list(inners or [])
        self._build_errors = list(build_errors or [])
        self.build_calls = []        # token dicts passed to build_client
        self.make_calls = []         # clients passed to make_source
        self.sleeps = []             # auth-retry sleep durations
        self.events = []             # (name, kwargs, connected-at-time)

    def build_client(self, token_dict):
        self.build_calls.append(token_dict)
        if self._build_errors:
            err = self._build_errors.pop(0)
            if err is not None:
                raise err
        return f"client{len(self.build_calls)}"

    def make_source(self, client):
        self.make_calls.append(client)
        return self._inners.pop(0)

    async def sleep(self, seconds):
        self.sleeps.append(seconds)

    def on_event(self, name, **kw):
        self.events.append((name, kw))

    def source(self, **overrides):
        kw = dict(token_source=self.token_source, build_client=self.build_client,
                  make_source=self.make_source, sleep_fn=self.sleep,
                  auth_retry_seconds=60.0, on_event=self.on_event)
        kw.update(overrides)
        return ReconnectingStreamSource(**kw)

    def event_names(self):
        return [n for n, _ in self.events]


async def collect(agen, n, timeout=2.0):
    out = []
    ai = agen.__aiter__()
    try:
        for _ in range(n):
            out.append(await asyncio.wait_for(ai.__anext__(), timeout))
    finally:
        await agen.aclose()
    return out


def run(coro):
    return asyncio.run(coro)


# --- tests ---------------------------------------------------------------

def test_first_connect_builds_client_with_fresh_token_and_streams():
    h = Harness(
        helper_responses=[(200, {"access_token": "A", "expires_at": NOW + 3600})],
        inners=[FakeInner([("tick", {"ts": 1, "price": 10.0, "size": 1}),
                           ("tick", {"ts": 2, "price": 10.1, "size": 1}),
                           ("hang", 5)]),
                FakeInner([("hang", 5)])],
    )
    src = h.source()
    ticks = run(collect(src.ticks("AEHL"), 2))
    assert [t["ts"] for t in ticks] == [1, 2]
    assert len(h.build_calls) == 1
    assert h.build_calls[0]["token"]["access_token"] == "A"
    assert h.make_calls == ["client1"]  # 2nd inner never built


def test_reconnects_when_inner_stream_ends():
    h = Harness(
        inners=[FakeInner([("tick", {"ts": 1}), ("tick", {"ts": 2}), ("end",)]),
                FakeInner([("tick", {"ts": 3}), ("tick", {"ts": 4}), ("hang", 5)])],
    )
    src = h.source()
    ticks = run(collect(src.ticks("X"), 4))
    assert [t["ts"] for t in ticks] == [1, 2, 3, 4]
    assert len(h.make_calls) == 2
    assert src.reconnect_count == 1
    assert "stream_ended" in h.event_names()
    assert "reconnect" in h.event_names()
    assert h.helper.calls == 2  # token refreshed before the reconnect


def test_reconnects_when_inner_stream_errors():
    h = Harness(
        inners=[FakeInner([("tick", {"ts": 1}), ("raise", RuntimeError("boom"))]),
                FakeInner([("tick", {"ts": 2}), ("hang", 5)])],
    )
    src = h.source()
    ticks = run(collect(src.ticks("X"), 2))
    assert [t["ts"] for t in ticks] == [1, 2]
    assert "stream_error" in h.event_names()
    assert len(h.make_calls) == 2


def test_proactive_refresh_fires_before_token_expiry():
    # First token is ~0.05s from its leeway window (expires_at = NOW + 300 + 0.05,
    # leeway 300). The first inner never yields, so the consume loop hits the
    # proactive-refresh deadline; the second token has a wide window.
    h = Harness(
        helper_responses=[
            (200, {"access_token": "A", "expires_at": NOW + 300 + 0.05}),
            (200, {"access_token": "B", "expires_at": NOW + 3600}),
        ],
        inners=[FakeInner([("hang", 10)]),
                FakeInner([("tick", {"ts": 1}), ("hang", 5)])],
    )
    src = h.source()
    ticks = run(collect(src.ticks("X"), 1))
    assert [t["ts"] for t in ticks] == [1]
    assert "proactive_refresh" in h.event_names()
    assert h.helper.calls == 2                       # re-fetched a fresh token
    assert len(h.build_calls) == 2
    assert h.build_calls[1]["token"]["access_token"] == "B"


def test_auth_required_does_not_crash_and_retries_after_sleep():
    h = Harness(
        helper_responses=[
            (409, {"error": "AUTH_REQUIRED", "message": "run bootstrap"}),
            (200, {"access_token": "A", "expires_at": NOW + 3600}),
        ],
        inners=[FakeInner([("tick", {"ts": 1}), ("hang", 5)])],
    )
    src = h.source()
    ticks = run(collect(src.ticks("X"), 1))
    assert [t["ts"] for t in ticks] == [1]
    assert h.sleeps == [60.0]
    assert "auth_error" in h.event_names()


def test_auth_helper_error_does_not_crash_and_retries():
    h = Harness(
        helper_responses=[
            (503, {"msg": "helper down"}),
            (200, {"access_token": "A", "expires_at": NOW + 3600}),
        ],
        inners=[FakeInner([("tick", {"ts": 1}), ("hang", 5)])],
    )
    src = h.source()
    ticks = run(collect(src.ticks("X"), 1))
    assert [t["ts"] for t in ticks] == [1]
    assert h.sleeps == [60.0]


def test_connected_is_false_after_generator_closed_mid_gap():
    h = Harness(
        inners=[FakeInner([("tick", {"ts": 1}), ("end",)]),
                FakeInner([("hang", 5)])],
    )
    src = h.source()
    run(collect(src.ticks("X"), 1))
    assert src.connected is False
