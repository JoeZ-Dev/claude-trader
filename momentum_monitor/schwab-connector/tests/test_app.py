import asyncio
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from app import create_app
from store import BarStore
from stream import ReplayStreamSource

RTH_1030 = 1756909800  # 2025-09-03 10:30:00 ET

FIXTURE_BARS = [
    {"ts": RTH_1030, "open": 10.0, "high": 10.6, "low": 9.9, "close": 10.4,
     "volume": 4000.0, "is_extended": False},
    {"ts": RTH_1030 + 10, "open": 10.4, "high": 10.9, "low": 10.3, "close": 10.7,
     "volume": 5200.0, "is_extended": False},
    {"ts": RTH_1030 + 20, "open": 10.7, "high": 10.8, "low": 10.1, "close": 10.2,
     "volume": 6100.0, "is_extended": False},
]


def _fixture(tmp_path):
    p = tmp_path / "replay.jsonl"
    p.write_text("".join(json.dumps(b) + "\n" for b in FIXTURE_BARS))
    return p


def _replay_factory(fx):
    """One shared ReplayStreamSource now serves every currently-watched
    symbol (fixed 2026-09-17 -- see specs.md), so source_factory takes a
    watched_symbols GETTER, not zero args."""
    return lambda watched_symbols: ReplayStreamSource(fx, watched_symbols=watched_symbols)


def _app(tmp_path, *, history_fetcher=None, daily_history_fetcher=None):
    fx = _fixture(tmp_path)
    store = BarStore(tmp_path / "bars")
    return create_app(
        store=store,
        source_factory=_replay_factory(fx),
        replay=True,
        now_fn=lambda: RTH_1030 + 30,
        history_fetcher=history_fetcher,
        daily_history_fetcher=daily_history_fetcher,
    ), store


def _wait_for_bars(client, symbol, want, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/bars/{symbol}")
        if len(r.json()) >= want:
            return r.json()
        time.sleep(0.05)
    return client.get(f"/bars/{symbol}").json()


def test_health_before_watch(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        body = c.get("/health").json()
        assert body["status"] == "ok"
        assert body["watching"] == []
        assert body["connected"] is False


def test_watch_then_bars_appear(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        assert c.post("/watch", json={"symbol": "AEHL"}).status_code == 200
        bars = _wait_for_bars(c, "AEHL", want=3)
        assert [b["ts"] for b in bars] == [b["ts"] for b in FIXTURE_BARS]
        assert bars[0]["close"] == 10.4
        assert set(bars[0].keys()) == {
            "ts", "open", "high", "low", "close", "volume", "is_extended"}


def test_bars_since_ts_filter(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        c.post("/watch", json={"symbol": "AEHL"})
        _wait_for_bars(c, "AEHL", want=3)
        r = c.get("/bars/AEHL", params={"since_ts": RTH_1030 + 10})
        assert [b["ts"] for b in r.json()] == [RTH_1030 + 10, RTH_1030 + 20]


def test_health_after_watch_reports_connected_and_watching(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        c.post("/watch", json={"symbol": "AEHL"})
        _wait_for_bars(c, "AEHL", want=1)
        body = c.get("/health").json()
        assert body["watching"] == ["AEHL"]
        assert body["connected"] is True


def test_bars_unknown_symbol_is_empty_200(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        r = c.get("/bars/ZZZZ")
        assert r.status_code == 200
        assert r.json() == []


def test_watch_is_idempotent(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        c.post("/watch", json={"symbol": "AEHL"})
        c.post("/watch", json={"symbol": "AEHL"})
        _wait_for_bars(c, "AEHL", want=1)
        assert c.get("/health").json()["watching"] == ["AEHL"]


def test_watch_requires_symbol(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        assert c.post("/watch", json={}).status_code == 422


def test_boots_without_working_source(tmp_path):
    def broken_factory(watched_symbols):
        raise RuntimeError("no schwab token on disk")

    app = create_app(
        store=BarStore(tmp_path / "bars"),
        source_factory=broken_factory,
        replay=False,
        now_fn=lambda: RTH_1030,
    )
    with TestClient(app) as c:
        assert c.post("/watch", json={"symbol": "AEHL"}).status_code == 200
        time.sleep(0.1)
        body = c.get("/health").json()
        assert body["status"] == "ok"
        assert body["watching"] == ["AEHL"]
        assert body["connected"] is False
        assert c.get("/bars/AEHL").json() == []


# -- one shared connection, not one per symbol ----------------------------

def test_watching_a_second_symbol_does_not_open_a_second_connection(tmp_path):
    fx = _fixture(tmp_path)
    factory_calls = []

    def factory(watched_symbols):
        factory_calls.append(1)
        return ReplayStreamSource(fx, watched_symbols=watched_symbols)

    app = create_app(
        store=BarStore(tmp_path / "bars"),
        source_factory=factory,
        replay=True,
        now_fn=lambda: RTH_1030 + 30,
    )
    with TestClient(app) as c:
        c.post("/watch", json={"symbol": "AEHL"})
        _wait_for_bars(c, "AEHL", want=1)
        c.post("/watch", json={"symbol": "SPY"})
        _wait_for_bars(c, "SPY", want=1)
    # Exactly ONE shared connection ever built, regardless of how many
    # symbols got watched -- the real point of this whole redesign
    # (specs.md: the old design built one independent connection PER
    # symbol, which throttled badly under real concurrent load).
    assert factory_calls == [1]


class _TrackingSharedSource:
    """A fake shared source that just hangs (never yields) but records
    add_symbol/remove_symbol calls -- for testing Connector's live-update
    passthrough in isolation from any real reconnect/replay machinery."""
    connected = True

    def __init__(self):
        self.add_calls = []
        self.remove_calls = []

    async def ticks(self):
        await asyncio.Event().wait()
        yield "", {}  # pragma: no cover -- never reached, just makes this a generator

    async def add_symbol(self, symbol):
        self.add_calls.append(symbol)

    async def remove_symbol(self, symbol):
        self.remove_calls.append(symbol)


def test_watching_a_second_symbol_after_connect_calls_add_symbol_live(tmp_path):
    shared = _TrackingSharedSource()
    app = create_app(
        store=BarStore(tmp_path / "bars"),
        source_factory=lambda watched_symbols: shared,
        replay=False,
        now_fn=lambda: RTH_1030,
    )
    with TestClient(app) as c:
        c.post("/watch", json={"symbol": "AEHL"})
        time.sleep(0.05)  # let _consume_shared start and set _shared_source
        c.post("/watch", json={"symbol": "SPY"})
        time.sleep(0.05)
    assert shared.add_calls == ["SPY"]


def test_unwatch_calls_remove_symbol_on_the_live_shared_source(tmp_path):
    shared = _TrackingSharedSource()
    app = create_app(
        store=BarStore(tmp_path / "bars"),
        source_factory=lambda watched_symbols: shared,
        replay=False,
        now_fn=lambda: RTH_1030,
    )
    with TestClient(app) as c:
        c.post("/watch", json={"symbol": "AEHL"})
        time.sleep(0.05)
        c.post("/unwatch", json={"symbol": "AEHL"})
    assert shared.remove_calls == ["AEHL"]


# -- tick heartbeat: confirms real ticks are (or aren't) actually flowing --

class _FakeLiveSource:
    """Minimal non-replay shared source: yields a fixed batch of (symbol,
    tick) pairs immediately, then hangs forever -- simulates a live
    connection that's genuinely healthy (never errors, never disconnects)
    but has simply stopped receiving anything further, the exact scenario
    the heartbeat log exists to make visible (found live 2026-09-17:
    aggregator and reconnect layers both reported healthy while real
    ticks had silently stopped arriving for a symbol Schwab was actively
    trading)."""
    connected = True

    def __init__(self, symbol, ticks):
        self._symbol = symbol
        self._ticks = ticks

    async def ticks(self):
        for t in self._ticks:
            yield self._symbol, t
        await asyncio.Event().wait()


def test_tick_heartbeat_reports_real_count_then_zero_when_stream_goes_quiet(tmp_path, caplog):
    ticks = [{"ts": RTH_1030 + i, "price": 10.0 + i * 0.01, "size": 5} for i in range(4)]
    app = create_app(
        store=BarStore(tmp_path / "bars"),
        source_factory=lambda watched_symbols: _FakeLiveSource("AEHL", ticks),
        replay=False,
        now_fn=lambda: RTH_1030 + 40,
        flush_interval=0.02,
        heartbeat_flush_cycles=2,
    )
    with caplog.at_level(logging.INFO, logger="schwab-connector.ticks"):
        with TestClient(app) as c:
            c.post("/watch", json={"symbol": "AEHL"})
            time.sleep(0.25)

    counts = [r.args[2] for r in caplog.records if "tick_heartbeat" in r.getMessage()]
    assert len(counts) >= 2, "expected multiple heartbeat cycles to fire"
    assert counts[0] == 4        # the 4 ticks fed right at connection start
    assert all(n == 0 for n in counts[1:])  # stream went quiet -- must show 0, not go silent


def test_unwatch_removes_symbol_from_watching_list(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        c.post("/watch", json={"symbol": "AEHL"})
        _wait_for_bars(c, "AEHL", want=1)
        assert c.get("/health").json()["watching"] == ["AEHL"]

        r = c.post("/unwatch", json={"symbol": "AEHL"})
        assert r.status_code == 200
        assert c.get("/health").json()["watching"] == []


def test_unwatch_only_removes_the_named_symbol(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        c.post("/watch", json={"symbol": "AEHL"})
        c.post("/watch", json={"symbol": "SPY"})
        _wait_for_bars(c, "AEHL", want=1)
        _wait_for_bars(c, "SPY", want=1)
        assert c.get("/health").json()["watching"] == ["AEHL", "SPY"]

        c.post("/unwatch", json={"symbol": "AEHL"})
        assert c.get("/health").json()["watching"] == ["SPY"]


def test_unwatch_unknown_symbol_is_a_noop_200(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        r = c.post("/unwatch", json={"symbol": "NOPE"})
        assert r.status_code == 200
        assert c.get("/health").json()["watching"] == []


def test_unwatch_is_case_insensitive(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        c.post("/watch", json={"symbol": "AEHL"})
        _wait_for_bars(c, "AEHL", want=1)
        c.post("/unwatch", json={"symbol": "aehl"})
        assert c.get("/health").json()["watching"] == []


def test_unwatch_then_rewatch_same_symbol_works(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        c.post("/watch", json={"symbol": "AEHL"})
        _wait_for_bars(c, "AEHL", want=1)
        c.post("/unwatch", json={"symbol": "AEHL"})
        assert c.get("/health").json()["watching"] == []

        c.post("/watch", json={"symbol": "AEHL"})
        assert c.get("/health").json()["watching"] == ["AEHL"]


def test_unwatch_requires_symbol(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        assert c.post("/unwatch", json={}).status_code == 422


def _backfill_bar(ts, close, *, vol=1000.0):
    return {"ts": ts, "open": close, "high": close, "low": close,
            "close": close, "volume": vol, "is_extended": False}


def test_watch_backfills_history_before_live_bars_appear(tmp_path):
    backfilled = [
        _backfill_bar(RTH_1030 - 1200, 9.0),
        _backfill_bar(RTH_1030 - 600, 9.5),
    ]
    calls = []

    async def history_fetcher(symbol):
        calls.append(symbol)
        return backfilled

    app, _ = _app(tmp_path, history_fetcher=history_fetcher)
    with TestClient(app) as c:
        c.post("/watch", json={"symbol": "AEHL"})
        bars = _wait_for_bars(c, "AEHL", want=len(backfilled) + 3)
        assert calls == ["AEHL"]
        assert [b["ts"] for b in bars] == \
            [b["ts"] for b in backfilled] + [b["ts"] for b in FIXTURE_BARS]
        assert bars[0]["close"] == 9.0


def test_watch_skips_backfill_when_symbol_already_has_bars(tmp_path):
    # Simulates a restart: the store already holds bars for this symbol
    # (from a previous backfill or live streaming), so re-fetching history
    # would be wasteful and risks the store's monotonic-append guard
    # silently dropping subsequent live bars whose ts falls behind a
    # re-fetched backfill bar's ts.
    store = BarStore(tmp_path / "bars")
    store.append("AEHL", _backfill_bar(RTH_1030 - 600, 8.0))

    def history_fetcher(symbol):
        raise AssertionError("history_fetcher must not be called for an "
                              "already-known symbol")

    fx = _fixture(tmp_path)
    app = create_app(
        store=store,
        source_factory=_replay_factory(fx),
        replay=True,
        now_fn=lambda: RTH_1030 + 30,
        history_fetcher=history_fetcher,
    )
    with TestClient(app) as c:
        c.post("/watch", json={"symbol": "AEHL"})
        bars = _wait_for_bars(c, "AEHL", want=1 + 3)
        assert bars[0]["ts"] == RTH_1030 - 600


def test_watch_backfill_failure_does_not_block_live_streaming(tmp_path):
    async def failing_history_fetcher(symbol):
        raise RuntimeError("companion-auth unreachable")

    app, _ = _app(tmp_path, history_fetcher=failing_history_fetcher)
    with TestClient(app) as c:
        c.post("/watch", json={"symbol": "AEHL"})
        bars = _wait_for_bars(c, "AEHL", want=3)
        assert [b["ts"] for b in bars] == [b["ts"] for b in FIXTURE_BARS]


# -- GET /daily_bars/{symbol} (session-level volume gate, specs.md
# section 12) -- an on-demand REST pass-through, not tied to watch/
# backfill lifecycle at all ------------------------------------------------

def test_daily_bars_calls_the_fetcher_and_returns_its_bars(tmp_path):
    daily_bars = [_backfill_bar(RTH_1030 - 86400 * i, 9.0 + i) for i in range(5)]
    calls = []

    async def daily_history_fetcher(symbol, *, lookback_days, include_today):
        calls.append((symbol, lookback_days, include_today))
        return daily_bars

    app, _ = _app(tmp_path, daily_history_fetcher=daily_history_fetcher)
    with TestClient(app) as c:
        r = c.get("/daily_bars/AEHL", params={"lookback_days": 20})
        assert r.status_code == 200
        body = r.json()
        assert body["symbol"] == "AEHL"
        assert body["bars"] == daily_bars
        assert calls == [("AEHL", 20, False)]  # include_today defaults False


def test_daily_bars_defaults_lookback_days_when_omitted(tmp_path):
    calls = []

    async def daily_history_fetcher(symbol, *, lookback_days, include_today):
        calls.append(lookback_days)
        return []

    app, _ = _app(tmp_path, daily_history_fetcher=daily_history_fetcher)
    with TestClient(app) as c:
        c.get("/daily_bars/AEHL")
        assert calls == [30]  # the documented default


def test_daily_bars_returns_503_when_no_fetcher_configured(tmp_path):
    app, _ = _app(tmp_path)  # daily_history_fetcher=None
    with TestClient(app) as c:
        r = c.get("/daily_bars/AEHL")
        assert r.status_code == 503
        assert r.json()["bars"] == []


def test_daily_bars_returns_502_on_fetcher_failure_not_a_crash(tmp_path):
    async def failing_fetcher(symbol, *, lookback_days, include_today):
        raise RuntimeError("companion-auth unreachable")

    app, _ = _app(tmp_path, daily_history_fetcher=failing_fetcher)
    with TestClient(app) as c:
        r = c.get("/daily_bars/AEHL")
        assert r.status_code == 502
        assert r.json()["bars"] == []


# -- include_today (specs.md section 23, market backdrop display) ---------
# forwards straight through to daily_history_fetcher; default False is the
# byte-for-byte pre-existing behavior every other caller depends on.

def test_daily_bars_forwards_include_today_true_when_requested(tmp_path):
    calls = []

    async def daily_history_fetcher(symbol, *, lookback_days, include_today):
        calls.append(include_today)
        return []

    app, _ = _app(tmp_path, daily_history_fetcher=daily_history_fetcher)
    with TestClient(app) as c:
        c.get("/daily_bars/SPY", params={"lookback_days": 2, "include_today": "true"})
        assert calls == [True]


def test_daily_bars_include_today_defaults_false_when_omitted(tmp_path):
    calls = []

    async def daily_history_fetcher(symbol, *, lookback_days, include_today):
        calls.append(include_today)
        return []

    app, _ = _app(tmp_path, daily_history_fetcher=daily_history_fetcher)
    with TestClient(app) as c:
        c.get("/daily_bars/AEHL")
        assert calls == [False]


# -- push (leg 1 of the poll -> push replacement, see specs.md) ----------

class _OneShotAgg:
    """A fake BarAggregator that hands back exactly one pre-built bar the
    first time drain() is called -- isolates _drain/_notify behavior from
    real feed/flush bucket mechanics, which is already covered above."""
    def __init__(self, bar):
        self._bars = [bar]

    def drain(self):
        out, self._bars = self._bars, []
        return out


def test_drain_notifies_subscriber_synchronously(tmp_path):
    """The whole point of push over poll: a subscriber sees a bar the
    instant _drain stores it, with no sleep/await needed to observe it."""
    from app import Connector

    store = BarStore(tmp_path / "bars")
    connector = Connector(store=store, source_factory=lambda w: None,
                          replay=True, now_fn=lambda: RTH_1030)
    bar = _backfill_bar(RTH_1030, 10.0)

    async def run():
        async with connector.subscribe() as q:
            connector._drain("AEHL", _OneShotAgg(bar))
            item = q.get_nowait()  # no await, no sleep
            assert item == {"symbol": "AEHL", "bar": bar}

    asyncio.run(run())


def test_subscribe_unregisters_on_exit(tmp_path):
    from app import Connector

    store = BarStore(tmp_path / "bars")
    connector = Connector(store=store, source_factory=lambda w: None,
                          replay=True, now_fn=lambda: RTH_1030)

    async def run():
        async with connector.subscribe():
            assert len(connector._subscribers) == 1
        assert len(connector._subscribers) == 0

    asyncio.run(run())


def test_slow_subscriber_drops_oldest_without_blocking_others_or_store(tmp_path):
    from app import Connector

    store = BarStore(tmp_path / "bars")
    connector = Connector(store=store, source_factory=lambda w: None,
                          replay=True, now_fn=lambda: RTH_1030,
                          subscriber_maxsize=2)
    bars = [_backfill_bar(RTH_1030 + i, 10.0 + i) for i in range(4)]

    async def run():
        async with connector.subscribe() as slow, connector.subscribe() as fast:
            for bar in bars[:3]:  # overflows slow's maxsize=2 by one
                connector._drain("AEHL", _OneShotAgg(bar))
                fast.get_nowait()  # keep fast fully drained
            # slow never drained -- must have dropped the OLDEST, not raised
            # or blocked, and the other subscriber + the store are unaffected
            got = [slow.get_nowait()["bar"]["ts"] for _ in range(2)]
            assert got == [bars[1]["ts"], bars[2]["ts"]]
            connector._drain("AEHL", _OneShotAgg(bars[3]))
            assert fast.get_nowait()["bar"]["ts"] == bars[3]["ts"]

    asyncio.run(run())
    assert [b["ts"] for b in store.since("AEHL", 0.0)] == [b["ts"] for b in bars]


async def _drive_streaming_route(app, path):
    """Drive an ASGI streaming route directly, bypassing
    fastapi.testclient's httpx ASGITransport -- which fully buffers a
    response (runs the whole ASGI app call to completion) before
    returning anything to the caller, so it can never observe partial
    output from a route that streams until client disconnect, which is
    exactly what /events (and monitor-app's /api/state/stream) do.
    Returns (task, chunks, disconnect) -- read body bytes off `chunks` as
    they're sent; set() `disconnect` and await `task` to end the drive."""
    chunks: asyncio.Queue = asyncio.Queue()
    disconnect = asyncio.Event()
    sent_body = False

    async def receive():
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body":
            await chunks.put(message.get("body", b""))

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "path": path, "raw_path": path.encode(),
        "query_string": b"", "headers": [], "scheme": "http",
        "server": ("testserver", 80), "client": ("testclient", 50000),
        "root_path": "",
    }
    task = asyncio.create_task(app(scope, receive, send))
    return task, chunks, disconnect


def test_events_route_delivers_bar_over_sse(tmp_path):
    app, _ = _app(tmp_path)

    async def run():
        task, chunks, disconnect = await _drive_streaming_route(app, "/events")
        try:
            first = await asyncio.wait_for(chunks.get(), timeout=2.0)
            assert first == b": connected\n\n"

            app.state.connector.watch("AEHL")  # same effect as POST /watch

            buf = b""
            while b"data:" not in buf:
                buf += await asyncio.wait_for(chunks.get(), timeout=2.0)
            line = next(l for l in buf.decode().split("\n") if l.startswith("data:"))
            payload = json.loads(line[len("data:"):].strip())
            assert payload["symbol"] == "AEHL"
            assert payload["bar"]["ts"] == FIXTURE_BARS[0]["ts"]
            assert payload["bar"]["close"] == FIXTURE_BARS[0]["close"]
        finally:
            disconnect.set()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(run())


def test_bars_survive_new_app_on_same_store_dir(tmp_path):
    app1, _ = _app(tmp_path)
    with TestClient(app1) as c:
        c.post("/watch", json={"symbol": "AEHL"})
        _wait_for_bars(c, "AEHL", want=3)

    # Fresh app + fresh BarStore over the same directory = a restart.
    fx = _fixture(tmp_path)
    app2 = create_app(
        store=BarStore(tmp_path / "bars"),
        source_factory=_replay_factory(fx),
        replay=True,
        now_fn=lambda: RTH_1030 + 30,
    )
    with TestClient(app2) as c:
        assert [b["ts"] for b in c.get("/bars/AEHL").json()] == \
            [b["ts"] for b in FIXTURE_BARS]
