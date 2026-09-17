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

    def __call__(self, url, headers=None):
        self.calls += 1
        if self.responses:
            return self.responses.pop(0)
        return 200, {"access_token": f"tok{self.calls}", "expires_at": NOW + 3600}


class FakeInner:
    """Fake stream source -- SchwabStreamSource's shape post-2026-09-17
    (one shared connection serving several symbols, not one per symbol).
    `script` is a list of:
        ("tick", {...})      -> yield a tick
        ("raise", exc)       -> raise
        ("end",)             -> stop (StopAsyncIteration)
        ("hang", seconds)    -> await sleep(seconds) before continuing
    ReconnectingStreamSource never interprets the yielded payload shape --
    it's pure passthrough -- so these stay bare dicts, matching the
    original tests; only the SUBSCRIBE side (symbols) changed shape.
    """

    def __init__(self, script):
        self._script = script
        self.connected = False
        self.ticks_calls = 0
        self.symbols = None
        self.add_calls = []
        self.remove_calls = []

    async def ticks(self, symbols):
        self.ticks_calls += 1
        self.symbols = symbols
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

    async def add_symbols(self, symbols):
        self.add_calls.append(symbols)

    async def remove_symbols(self, symbols):
        self.remove_calls.append(symbols)


class Harness:
    def __init__(self, helper_responses=(), inners=None, build_errors=None,
                watched_symbols=None):
        self.helper = FakeHelper(*helper_responses)
        self.token_source = AccessTokenSource("http://companion-auth:9999",
                                              http_get=self.helper,
                                              now_fn=lambda: NOW)
        self._inners = list(inners or [])
        self._build_errors = list(build_errors or [])
        self.build_calls = []        # token dicts passed to build_client
        self.make_calls = []         # clients passed to make_source
        self.sleeps = []             # auth-retry sleep durations
        self.events = []             # (name, kwargs)
        self._watched_symbols = watched_symbols or (lambda: {"AEHL"})

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
                  auth_retry_seconds=60.0, on_event=self.on_event,
                  watched_symbols=self._watched_symbols)
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
    ticks = run(collect(src.ticks(), 2))
    assert [t["ts"] for t in ticks] == [1, 2]
    assert len(h.build_calls) == 1
    assert h.build_calls[0]["token"]["access_token"] == "A"
    assert h.make_calls == ["client1"]  # 2nd inner never built


def test_ticks_subscribes_to_the_current_watched_symbols():
    # The core new behavior this whole module change exists for: the
    # symbol set is read FRESH from watched_symbols() at connect time,
    # not frozen at construction -- so a symbol added/removed between
    # reconnects is picked up automatically on the next connect with no
    # separate "pending changes" bookkeeping.
    calls = {"n": 0}

    def watched():
        calls["n"] += 1
        return {"AEHL", "DAIC"} if calls["n"] == 1 else {"AEHL", "WETO"}

    inner1 = FakeInner([("tick", {"ts": 1}), ("end",)])
    inner2 = FakeInner([("tick", {"ts": 2}), ("hang", 5)])
    h = Harness(watched_symbols=watched, inners=[inner1, inner2])
    src = h.source()
    run(collect(src.ticks(), 2))
    assert inner1.symbols == {"AEHL", "DAIC"}
    assert inner2.symbols == {"AEHL", "WETO"}


def test_reconnects_when_inner_stream_ends():
    h = Harness(
        inners=[FakeInner([("tick", {"ts": 1}), ("tick", {"ts": 2}), ("end",)]),
                FakeInner([("tick", {"ts": 3}), ("tick", {"ts": 4}), ("hang", 5)])],
    )
    src = h.source()
    ticks = run(collect(src.ticks(), 4))
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
    ticks = run(collect(src.ticks(), 2))
    assert [t["ts"] for t in ticks] == [1, 2]
    assert "stream_error" in h.event_names()
    assert len(h.make_calls) == 2


def test_add_symbol_forwards_to_the_live_inner_source():
    inner = FakeInner([("hang", 5)])
    h = Harness(inners=[inner])
    src = h.source()

    async def drive():
        agen = src.ticks().__aiter__()
        # Let the connect sequence run far enough to set up the inner source.
        task = asyncio.ensure_future(agen.__anext__())
        await asyncio.sleep(0.01)
        await src.add_symbol("DAIC")
        await src.remove_symbol("AEHL")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await agen.aclose()

    run(drive())
    assert inner.add_calls == [["DAIC"]]
    assert inner.remove_calls == [["AEHL"]]


def test_add_symbol_is_a_noop_when_nothing_is_currently_connected():
    # No inner source live yet (e.g. mid auth-retry backoff) -- must not
    # raise; the change is picked up on the next connect via
    # watched_symbols() instead.
    h = Harness(helper_responses=[(409, {"error": "AUTH_REQUIRED", "message": "x"})])
    src = h.source()
    run(src.add_symbol("DAIC"))
    run(src.remove_symbol("AEHL"))


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
    ticks = run(collect(src.ticks(), 1))
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
    ticks = run(collect(src.ticks(), 1))
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
    ticks = run(collect(src.ticks(), 1))
    assert [t["ts"] for t in ticks] == [1]
    assert h.sleeps == [60.0]


def test_backs_off_when_a_fresh_refresh_is_immediately_stale_again():
    # Reproduces a real incident (2026-09-16): companion-auth served an
    # already-near-expiry (per this side's leeway) token on every refresh
    # for a full hour. Without a backoff, budget<=0 fires before a single
    # tick is ever consumed, and the outer loop immediately refreshes and
    # reconnects again with no delay -- confirmed live as ~13 reconnects/sec
    # against companion-auth, sustained, not a one-off blip.
    calls = {"n": 0}

    def always_near_expiry_helper(url, headers=None):
        calls["n"] += 1
        # leeway=300 (AccessTokenSource default) -> stale_at() = NOW+100-300
        # = NOW-200, already stale the instant it's fetched.
        return 200, {"access_token": f"tok{calls['n']}", "expires_at": NOW + 100}

    token_source = AccessTokenSource("http://companion-auth:9999",
                                     http_get=always_near_expiry_helper,
                                     now_fn=lambda: NOW)
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    events = []
    src = ReconnectingStreamSource(
        token_source=token_source,
        build_client=lambda token: object(),
        make_source=lambda client: FakeInner([("hang", 10)]),
        watched_symbols=lambda: {"X"},
        sleep_fn=fake_sleep,
        auth_retry_seconds=60.0,
        on_event=lambda name, **kw: events.append(name),
    )

    async def drive():
        agen = src.ticks().__aiter__()
        try:
            await asyncio.wait_for(agen.__anext__(), timeout=0.2)
        except asyncio.TimeoutError:
            pass
        finally:
            await agen.aclose()

    run(drive())

    assert calls["n"] > 1, "expected more than one refresh within the window"
    assert sleeps, "expected a backoff sleep between zero-tick reconnect cycles"
    assert all(s == 60.0 for s in sleeps)
    assert "stale_immediately_after_refresh" in events


def test_connected_is_false_after_generator_closed_mid_gap():
    h = Harness(
        inners=[FakeInner([("tick", {"ts": 1}), ("end",)]),
                FakeInner([("hang", 5)])],
    )
    src = h.source()
    run(collect(src.ticks(), 1))
    assert src.connected is False
