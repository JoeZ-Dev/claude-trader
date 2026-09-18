import asyncio
import json
import os
import sys
import threading
import time

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _APP_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(_APP_DIR), "core"))

from fastapi.testclient import TestClient

from app import _journal_closed_rows_html, create_app

RTH = 1756909800  # 2025-09-03 10:30:00 ET


def _bars(n, start=RTH, base=10.0):
    out = []
    for i in range(n):
        p = base + (i % 5) * 0.1
        out.append({"ts": start + i * 10, "open": p, "high": p + 0.15,
                    "low": p - 0.15, "close": p, "volume": 1000.0 + i,
                    "is_extended": False})
    return out


class FakeFetch:
    """Async stand-in for the httpx call to schwab-connector /bars.

    Phase 2: multiple symbols are polled independently, so batches are
    queued PER SYMBOL -- each call for a given symbol pops that symbol's
    own next queued batch; once a symbol's queue is exhausted, further
    calls for it just return [] (a real, harmless "nothing new" poll, not
    an error)."""

    def __init__(self, batches_by_symbol=None):
        self._queues = {k.upper(): list(v) for k, v in (batches_by_symbol or {}).items()}
        self.calls = []          # (symbol, since_ts) pairs
        self.raise_next_for = None

    def queue_more(self, symbol, batch):
        """Append one more batch for `symbol`, to be popped by the NEXT
        call -- lets a test add data mid-run (e.g. simulating "the store
        now has one more bar") without racing a batch already queued at
        construction against whatever triggers the next real call."""
        self._queues.setdefault(symbol.upper(), []).append(list(batch))

    async def __call__(self, symbol, since_ts):
        self.calls.append((symbol, since_ts))
        if self.raise_next_for == symbol:
            self.raise_next_for = None
            raise RuntimeError("connector unreachable")
        q = self._queues.get(symbol)
        if not q:
            return []
        batch = q.pop(0)
        return [b for b in batch if b["ts"] >= since_ts]


class FakeStreamEvents:
    """Test double for create_app's stream_events dependency (main.py's
    real one consumes schwab-connector's shared SSE connection). Lets a
    test push a bar event into the running Poller from the synchronous
    test thread, bridged onto the app's own event loop via
    call_soon_threadsafe (NOT run_in_executor on a blocking stdlib Queue
    -- that leaves a worker thread parked forever in a blocking get(),
    which then hangs the event loop's shutdown_default_executor() at
    TestClient teardown, since asyncio can't interrupt a real OS thread's
    blocking call the way it can cancel a coroutine awaiting an
    asyncio.Queue). Mirrors how a real push arrives asynchronously,
    without needing a live network or TestClient's (fully-buffering, see
    schwab-connector's own /events tests) HTTP layer."""

    def __init__(self):
        self._loop = None
        self._queue = None
        self.reconnects = 0

    def push_bar(self, symbol, bar):
        while self._loop is None:  # __call__ hasn't started yet -- brief, bounded wait
            time.sleep(0.005)
        self._loop.call_soon_threadsafe(self._queue.put_nowait, (symbol, bar))

    async def __call__(self, on_bar, on_reconnect):
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        await on_reconnect()
        self.reconnects += 1
        while True:
            symbol, bar = await self._queue.get()
            await on_bar(symbol, bar)


def _wait_until(pred, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.03)
    return pred()


def _client(fetch, *, symbol="AEHL", announce=None, unwatch=None,
            announce_retry_attempts=5, announce_retry_base_delay=0.02,
            announce_retry_max_delay=0.02, journal_store=None,
            trail_pct=0.05, max_symbols=4, stream_events=None):
    app = create_app(fetch_bars=fetch, watch_symbol=symbol,
                     announce_watch=announce,
                     announce_unwatch=unwatch,
                     announce_retry_attempts=announce_retry_attempts,
                     announce_retry_base_delay=announce_retry_base_delay,
                     announce_retry_max_delay=announce_retry_max_delay,
                     journal_store=journal_store, trail_pct=trail_pct,
                     max_symbols=max_symbols, stream_events=stream_events)
    return TestClient(app)


def _sym_state(client, symbol):
    return client.get("/api/state").json()["symbols"].get(symbol)


# -- /api/state shape ------------------------------------------------------

def test_api_state_shape_has_symbols_recent_closed_poll_enabled_max_symbols():
    with _client(FakeFetch({"AEHL": [_bars(3)]})) as c:
        body = c.get("/api/state").json()
        assert set(body) == {"symbols", "recent_closed", "poll_enabled",
                             "max_symbols", "strategy_params"}
        assert isinstance(body["symbols"], dict)
        assert isinstance(body["recent_closed"], list)
        assert body["max_symbols"] == 4


def test_no_symbol_configured_stays_idle():
    fetch = FakeFetch({"AEHL": [_bars(10)]})
    with _client(fetch, symbol=None) as c:
        time.sleep(0.2)
        body = c.get("/api/state").json()
        assert body["symbols"] == {}
        assert fetch.calls == []


def test_api_state_warming_up_before_any_bars():
    with _client(FakeFetch({})) as c:
        assert _wait_until(lambda: "AEHL" in c.get("/api/state").json()["symbols"])
        assert _sym_state(c, "AEHL")["status"] == "warming_up"


def test_api_state_reflects_fetched_bars():
    bars = _bars(30)
    with _client(FakeFetch({"AEHL": [bars]})) as c:
        assert _wait_until(lambda: (_sym_state(c, "AEHL") or {}).get("status") == "ok")
        st = _sym_state(c, "AEHL")
        assert st["bar_count"] == 30
        assert st["symbol"] == "AEHL"
        assert st["last_price"] == round(bars[-1]["close"], 4)


def test_poller_advances_since_ts_and_dedups_boundary_bar():
    first = _bars(20)
    overlap = first[-1]
    second = [overlap] + _bars(5, start=overlap["ts"] + 10, base=11.0)
    fetch = FakeFetch({"AEHL": [first, second]})
    with _client(fetch) as c:
        assert _wait_until(lambda: (_sym_state(c, "AEHL") or {}).get("bar_count", 0) >= 20)
        # No timer re-fetches on its own anymore -- pause+resume (resync_all)
        # is what pulls the second, overlapping batch in, the same way a
        # real resync after a schwab-connector /events reconnect would.
        c.post("/api/polling", json={"enabled": False})
        c.post("/api/polling", json={"enabled": True})
        st = _sym_state(c, "AEHL")
        assert st["bar_count"] == 25
        assert any(s > 0 for sym, s in fetch.calls if sym == "AEHL")


def test_api_state_survives_fetch_error():
    fetch = FakeFetch({"AEHL": [_bars(20), _bars(5, start=RTH + 200, base=12.0)]})
    with _client(fetch) as c:
        assert _wait_until(lambda: (_sym_state(c, "AEHL") or {}).get("status") == "ok")
        fetch.raise_next_for = "AEHL"
        time.sleep(0.2)
        r = c.get("/api/state")
        assert r.status_code == 200
        assert _sym_state(c, "AEHL")["status"] == "ok"  # last good state retained


# -- announce_watch on startup / retry --------------------------------------

def test_announce_watch_called_on_startup():
    seen = []

    async def announce(sym):
        seen.append(sym)

    with _client(FakeFetch({"AEHL": [_bars(5)]}), announce=announce) as c:
        assert _wait_until(lambda: seen == ["AEHL"])


def test_announce_watch_retries_and_recovers_after_transient_failures():
    calls = []

    async def flaky_announce(sym):
        calls.append(sym)
        if len(calls) < 3:
            raise RuntimeError("connector not listening yet")

    with _client(FakeFetch({"AEHL": [_bars(5)]}), announce=flaky_announce) as c:
        assert _wait_until(lambda: len(calls) == 3)
        assert _wait_until(lambda: (_sym_state(c, "AEHL") or {}).get("status") == "ok")


def test_announce_watch_gives_up_after_exhausting_retries_but_keeps_polling():
    async def always_fails(sym):
        raise RuntimeError("connector unreachable")

    fetch = FakeFetch({"AEHL": [_bars(5)]})
    with _client(fetch, announce=always_fails, announce_retry_attempts=2) as c:
        assert _wait_until(lambda: (_sym_state(c, "AEHL") or {}).get("status") == "ok")


# -- adding/removing symbols (Stage A core) ---------------------------------

def test_post_watch_adds_a_new_symbol():
    seen = []

    async def announce(sym):
        seen.append(sym)

    fetch = FakeFetch({"AEHL": [_bars(3)], "MSFT": [_bars(3, base=50.0)]})
    with _client(fetch, announce=announce) as c:
        assert _wait_until(lambda: seen == ["AEHL"])
        r = c.post("/api/watch", data={"symbol": "msft"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["symbols"] == ["AEHL", "MSFT"]
        assert _wait_until(lambda: (_sym_state(c, "MSFT") or {}).get("status") == "ok")
        # AEHL must still be there too -- adding never replaces
        assert _wait_until(lambda: (_sym_state(c, "AEHL") or {}).get("status") == "ok")


def test_post_watch_rejects_duplicate_symbol():
    with _client(FakeFetch({"AEHL": [_bars(3)]})) as c:
        assert _wait_until(lambda: "AEHL" in c.get("/api/state").json()["symbols"])
        r = c.post("/api/watch", data={"symbol": "aehl"})
        assert r.status_code == 409
        body = r.json()
        assert body["ok"] is False
        assert "already" in body["reason"].lower()
        assert body["symbols"] == ["AEHL"]  # unchanged, not silently replaced


def test_post_watch_rejects_invalid_symbol():
    with _client(FakeFetch({})) as c:
        r = c.post("/api/watch", data={"symbol": "not valid!"})
        assert r.status_code == 409
        assert r.json()["ok"] is False


def test_post_watch_rejects_blank_symbol():
    with _client(FakeFetch({})) as c:
        r = c.post("/api/watch", data={"symbol": "   "})
        assert r.status_code == 409
        assert r.json()["ok"] is False


def test_post_watch_at_capacity_evicts_the_oldest_symbol_not_a_rejection():
    unwatched = []

    async def unwatch(sym):
        unwatched.append(sym)

    fetch = FakeFetch({s: [_bars(2, base=float(i))] for i, s in
                       enumerate(["AEHL", "S2", "S3", "S4", "S5"])})
    with _client(fetch, symbol="AEHL", unwatch=unwatch) as c:
        for sym in ("S2", "S3", "S4"):
            assert c.post("/api/watch", data={"symbol": sym}).json()["ok"] is True
        assert set(c.get("/api/state").json()["symbols"]) == {"AEHL", "S2", "S3", "S4"}

        # AEHL was added first, so it's the one that gets dropped to make
        # room for S5 -- a 200, not a 409, with the eviction reported back
        # in "reason" rather than silently.
        r = c.post("/api/watch", data={"symbol": "S5"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert "AEHL" in body["reason"]
        assert set(body["symbols"]) == {"S2", "S3", "S4", "S5"}
        assert _wait_until(lambda: unwatched == ["AEHL"])
        assert "AEHL" not in c.get("/api/state").json()["symbols"]


def test_post_watch_eviction_force_closes_the_evicted_symbols_open_position():
    class FakeJournalStore:
        def __init__(self):
            self.closed = []

        def open_position_for(self, symbol):
            return None

        def close_position(self, position, exit_event):
            self.closed.append((position.symbol, exit_event.exit_reason))

        def recent_closed(self, limit=10):
            return []

        def get_param(self, key, default):
            return default

        def all_params(self):
            return {}

    from journal_logic import OpenPosition

    fetch = FakeFetch({s: [_bars(2, base=float(i))] for i, s in
                       enumerate(["AEHL", "S2", "S3", "S4", "S5"])})
    store = FakeJournalStore()
    with _client(fetch, symbol="AEHL", journal_store=store) as c:
        for sym in ("S2", "S3", "S4"):
            assert c.post("/api/watch", data={"symbol": sym}).json()["ok"] is True
        assert _wait_until(lambda: "AEHL" in c.get("/api/state").json()["symbols"])

        # Simulate AEHL having an open virtual position at eviction time --
        # eviction must force-close it exactly like an explicit unwatch does.
        app_obj = c.app
        poller = app_obj.state.poller
        poller._slots["AEHL"].journal_position = OpenPosition(
            id=1, symbol="AEHL", entry_ts=0, entry_price=10.0,
            high_water_mark=10.0, stop_level=9.0,
        )

        r = c.post("/api/watch", data={"symbol": "S5"})
        assert r.json()["ok"] is True
        assert store.closed == [("AEHL", "symbol_switched")]


# -- deleting journal rows: /api/journal/delete, /api/journal/clear_symbol_switched --

def _seed_closed_trades(store):
    """Real JournalStore (SQLite over tmp_path), not a fake -- the delete
    endpoints' own SQL is already unit-tested in test_journal_store.py;
    these tests only need to prove the HTTP wiring on top of it."""
    from journal_logic import ExitEvent

    p1 = store.create(_position(symbol="AEHL"))
    store.close_position(p1, ExitEvent(exit_ts=100, exit_price=11.0,
                                       exit_reason="trailing_stop"))
    p2 = store.create(_position(symbol="MSFT"))
    store.close_position(p2, ExitEvent(exit_ts=200, exit_price=9.0,
                                       exit_reason="symbol_switched"))
    p3 = store.create(_position(symbol="NVDA"))
    store.close_position(p3, ExitEvent(exit_ts=300, exit_price=12.0,
                                       exit_reason="symbol_switched"))
    return store.recent_closed()


def _position(symbol, entry_ts=0, entry_price=10.0, high_water_mark=10.0, stop_level=9.5):
    from journal_logic import OpenPosition
    return OpenPosition(id=None, symbol=symbol, entry_ts=entry_ts,
                        entry_price=entry_price, high_water_mark=high_water_mark,
                        stop_level=stop_level)


def test_post_journal_delete_removes_one_closed_row(tmp_path):
    from journal_store import JournalStore
    store = JournalStore(tmp_path / "journal.db")
    closed = _seed_closed_trades(store)
    target_id = next(c["id"] for c in closed if c["symbol"] == "AEHL")

    with _client(FakeFetch({}), symbol=None, journal_store=store) as c:
        r = c.post("/api/journal/delete", data={"id": str(target_id)})
        assert r.status_code == 200
        assert r.json()["ok"] is True

    remaining = {c["symbol"] for c in store.recent_closed()}
    assert remaining == {"MSFT", "NVDA"}


def test_post_journal_delete_unknown_id_returns_404(tmp_path):
    from journal_store import JournalStore
    store = JournalStore(tmp_path / "journal.db")
    with _client(FakeFetch({}), symbol=None, journal_store=store) as c:
        r = c.post("/api/journal/delete", data={"id": "999999"})
        assert r.status_code == 404
        assert r.json()["ok"] is False


def test_post_journal_delete_non_numeric_id_returns_409():
    with _client(FakeFetch({}), symbol=None) as c:
        r = c.post("/api/journal/delete", data={"id": "not-a-number"})
        assert r.status_code == 409
        assert r.json()["ok"] is False


def test_post_journal_clear_symbol_switched_removes_only_those_rows(tmp_path):
    from journal_store import JournalStore
    store = JournalStore(tmp_path / "journal.db")
    _seed_closed_trades(store)

    with _client(FakeFetch({}), symbol=None, journal_store=store) as c:
        r = c.post("/api/journal/clear_symbol_switched")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["deleted"] == 2

    remaining = store.recent_closed()
    assert [c["symbol"] for c in remaining] == ["AEHL"]
    assert remaining[0]["exit_reason"] == "trailing_stop"


def test_post_unwatch_removes_a_symbol():
    unwatched = []

    async def unwatch(sym):
        unwatched.append(sym)

    fetch = FakeFetch({"AEHL": [_bars(3)]})
    with _client(fetch, unwatch=unwatch) as c:
        assert _wait_until(lambda: "AEHL" in c.get("/api/state").json()["symbols"])
        r = c.post("/api/unwatch", data={"symbol": "aehl"})
        assert r.status_code == 200
        body = r.json()
        assert body["removed"] is True
        assert body["symbols"] == []
        assert _wait_until(lambda: unwatched == ["AEHL"])
        assert "AEHL" not in c.get("/api/state").json()["symbols"]


def test_post_unwatch_unknown_symbol_is_a_noop_not_an_error():
    with _client(FakeFetch({"AEHL": [_bars(3)]})) as c:
        r = c.post("/api/unwatch", data={"symbol": "NOPE"})
        assert r.status_code == 200
        assert r.json()["removed"] is False


def test_removing_a_symbol_frees_a_slot_without_triggering_eviction():
    fetch = FakeFetch({s: [_bars(2, base=float(i))] for i, s in
                       enumerate(["AEHL", "S2", "S3", "S4", "S5"])})
    with _client(fetch, symbol="AEHL") as c:
        for sym in ("S2", "S3", "S4"):
            c.post("/api/watch", data={"symbol": sym})

        # S2 (not the oldest) is explicitly unwatched first, freeing a slot
        # -- the next add should just fill it, not evict anything else.
        c.post("/api/unwatch", data={"symbol": "S2"})
        r = c.post("/api/watch", data={"symbol": "S5"})
        assert r.json()["ok"] is True
        assert r.json()["reason"] == ""  # a free slot was already there, no eviction
        assert set(r.json()["symbols"]) == {"AEHL", "S3", "S4", "S5"}


# -- 4 concurrent symbols: independent state, one's failure doesn't leak --

def test_four_symbols_tracked_concurrently_with_independent_state():
    fetch = FakeFetch({
        "AEHL": [_bars(10, base=10.0)],
        "S2": [_bars(10, base=20.0)],
        "S3": [_bars(10, base=30.0)],
        "S4": [_bars(10, base=40.0)],
    })
    with _client(fetch, symbol="AEHL") as c:
        for sym in ("S2", "S3", "S4"):
            assert c.post("/api/watch", data={"symbol": sym}).json()["ok"] is True

        assert _wait_until(lambda: all(
            (_sym_state(c, s) or {}).get("status") == "ok"
            for s in ("AEHL", "S2", "S3", "S4")
        ))
        symbols = c.get("/api/state").json()["symbols"]
        # each symbol's last_price reflects ITS OWN base, not another's --
        # the exact "one symbol's data leaking into another's" check.
        assert symbols["AEHL"]["last_price"] in (10.0, 10.1, 10.2, 10.3, 10.4)
        assert symbols["S2"]["last_price"] in (20.0, 20.1, 20.2, 20.3, 20.4)
        assert symbols["S3"]["last_price"] in (30.0, 30.1, 30.2, 30.3, 30.4)
        assert symbols["S4"]["last_price"] in (40.0, 40.1, 40.2, 40.3, 40.4)


def test_one_symbols_fetch_error_does_not_affect_others():
    fetch = FakeFetch({
        "AEHL": [_bars(10, base=10.0), _bars(3, start=RTH + 200, base=10.5)],
        "S2": [_bars(10, base=20.0)],
    })
    with _client(fetch, symbol="AEHL") as c:
        c.post("/api/watch", data={"symbol": "S2"})
        assert _wait_until(lambda: all(
            (_sym_state(c, s) or {}).get("status") == "ok" for s in ("AEHL", "S2")
        ))
        fetch.raise_next_for = "AEHL"
        # Pause+resume is what actually triggers a fresh catch_up (via
        # resync_all) now that there's no timer re-fetching on its own --
        # this is what exercises AEHL's fetch error and confirms S2 (whose
        # queued batch is already exhausted, so its own catch_up call is a
        # harmless no-op) is untouched by it.
        c.post("/api/polling", json={"enabled": False})
        c.post("/api/polling", json={"enabled": True})
        time.sleep(0.2)
        # AEHL keeps serving its last good state; S2 is untouched throughout
        assert _sym_state(c, "AEHL")["status"] == "ok"
        assert _sym_state(c, "S2")["status"] == "ok"
        assert _sym_state(c, "S2")["bar_count"] == 10


def test_removing_one_symbol_does_not_disturb_others():
    fetch = FakeFetch({
        "AEHL": [_bars(5, base=10.0)],
        "S2": [_bars(5, base=20.0)],
    })
    with _client(fetch, symbol="AEHL") as c:
        c.post("/api/watch", data={"symbol": "S2"})
        assert _wait_until(lambda: all(
            (_sym_state(c, s) or {}).get("status") == "ok" for s in ("AEHL", "S2")
        ))
        c.post("/api/unwatch", data={"symbol": "AEHL"})
        time.sleep(0.15)
        body = c.get("/api/state").json()
        assert "AEHL" not in body["symbols"]
        assert body["symbols"]["S2"]["status"] == "ok"
        assert body["symbols"]["S2"]["bar_count"] == 5


def test_remove_then_readd_during_inflight_poll_discards_stale_result():
    # Generalizes phase 1's switch_symbol race test to per-symbol slots:
    # a remove_symbol() (and re-add) landing while a poll for that exact
    # symbol is still in flight must not let the stale fetch's result get
    # applied to the NEW slot's (freshly-reset) bar list.
    release = threading.Event()
    calls = []

    async def fetch(symbol, since_ts):
        calls.append((symbol, since_ts))
        if symbol == "AEHL" and len(calls) == 1:
            while not release.is_set():
                await asyncio.sleep(0.01)
            return _bars(5)  # stale by the time it returns -- must be discarded
        if symbol == "AEHL":
            return _bars(3, base=99.0)  # the RE-ADDED slot's real data
        return []

    with _client(fetch, symbol="AEHL") as c:
        time.sleep(0.1)  # let the first (now-blocked) AEHL fetch start
        c.post("/api/unwatch", data={"symbol": "AEHL"})
        c.post("/api/watch", data={"symbol": "AEHL"})
        release.set()
        assert _wait_until(lambda: (_sym_state(c, "AEHL") or {}).get("status") == "ok")
        assert _sym_state(c, "AEHL")["bar_count"] == 3
        assert _sym_state(c, "AEHL")["last_price"] == 99.2  # _bars(3, base=99.0)'s last close


# -- server-side polling pause (POST /api/polling), generalized ------------

def test_api_state_exposes_poll_enabled_default_true():
    with _client(FakeFetch({"AEHL": [_bars(5)]})) as c:
        assert c.get("/api/state").json()["poll_enabled"] is True


def test_poll_control_pauses_applying_pushed_bars_for_all_symbols():
    fetch = FakeFetch({"AEHL": [_bars(3)], "S2": [_bars(3, base=20.0)]})
    fse = FakeStreamEvents()
    with _client(fetch, symbol="AEHL", stream_events=fse) as c:
        c.post("/api/watch", data={"symbol": "S2"})
        assert _wait_until(lambda: all(
            (_sym_state(c, s) or {}).get("status") == "ok" for s in ("AEHL", "S2")
        ))
        r = c.post("/api/polling", json={"enabled": False})
        assert r.json()["poll_enabled"] is False

        pushed = _bars(1, start=RTH + 1000, base=50.0)[0]
        fse.push_bar("AEHL", pushed)
        time.sleep(0.1)
        assert _sym_state(c, "AEHL")["bar_count"] == 3, \
            "a pushed bar was applied while paused -- polling didn't stop for all symbols"
        assert _sym_state(c, "S2")["bar_count"] == 3  # untouched either way


def test_poll_control_resumes_and_resyncs_what_was_dropped_while_paused():
    # The REST endpoint a real resync hits always reflects schwab-
    # connector's current stored history, so the bar dropped while paused
    # is exactly what a resync's catch_up fetch would return.
    dropped = _bars(1, start=RTH + 1000, base=50.0)[0]
    fetch = FakeFetch({"AEHL": [_bars(3)]})
    fse = FakeStreamEvents()
    with _client(fetch, stream_events=fse) as c:
        assert _wait_until(lambda: (_sym_state(c, "AEHL") or {}).get("status") == "ok")
        # fse's on_reconnect fires resync_all() once already, right at
        # stream startup (same as a real reconnect would) -- queuing the
        # "dropped" batch only now, after that's had time to settle,
        # avoids racing it against the startup resync.
        time.sleep(0.05)
        c.post("/api/polling", json={"enabled": False})

        fse.push_bar("AEHL", dropped)
        time.sleep(0.1)
        assert _sym_state(c, "AEHL")["bar_count"] == 3  # dropped while paused

        fetch.queue_more("AEHL", [dropped])  # what schwab-connector's store now has
        r = c.post("/api/polling", json={"enabled": True})
        assert r.json()["poll_enabled"] is True
        assert _sym_state(c, "AEHL")["bar_count"] == 4  # resync_all() caught it up
        assert _sym_state(c, "AEHL")["last_price"] == round(dropped["close"], 4)


# -- live-tunable strategy parameters, specs.md section 8 ------------------

def _client_with_real_store(fetch, tmp_path, *, symbol="AEHL", default_params=None):
    from journal_store import JournalStore
    store = JournalStore(tmp_path / "journal.db", default_params=default_params or {
        "trail_pct": 0.05, "volume_confirm_threshold": 1.5,
    })
    return _client(fetch, symbol=symbol, journal_store=store), store


def test_get_strategy_params_lists_current_values_and_history(tmp_path):
    c, store = _client_with_real_store(FakeFetch({}), tmp_path)
    with c:
        body = c.get("/api/strategy_params").json()
        assert body["params"]["trail_pct"]["value"] == 0.05
        assert body["params"]["volume_confirm_threshold"]["value"] == 1.5
        assert body["history"] == []


def test_post_strategy_params_updates_a_value_and_is_reflected_on_get(tmp_path):
    c, store = _client_with_real_store(FakeFetch({}), tmp_path)
    with c:
        r = c.post("/api/strategy_params", json={"trail_pct": 0.08})
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert r.json()["updated"] == {"trail_pct": 0.08}

        body = c.get("/api/strategy_params").json()
        assert body["params"]["trail_pct"]["value"] == 0.08


def test_post_strategy_params_records_the_change_in_history(tmp_path):
    c, store = _client_with_real_store(FakeFetch({}), tmp_path)
    with c:
        c.post("/api/strategy_params", json={"trail_pct": 0.08})
        history = c.get("/api/strategy_params").json()["history"]
        assert len(history) == 1
        assert history[0]["key"] == "trail_pct"
        assert history[0]["old_value"] == 0.05
        assert history[0]["new_value"] == 0.08
        assert history[0]["changed_at"] is not None


def test_post_strategy_params_rejects_an_invalid_value_and_changes_nothing(tmp_path):
    c, store = _client_with_real_store(FakeFetch({}), tmp_path)
    with c:
        r = c.post("/api/strategy_params", json={"trail_pct": -1.0})
        assert r.status_code == 409
        assert r.json()["ok"] is False
        assert "trail_pct" in r.json()["rejected"]

        body = c.get("/api/strategy_params").json()
        assert body["params"]["trail_pct"]["value"] == 0.05  # unchanged
        assert body["history"] == []  # no trail left by a rejected change


def test_post_strategy_params_can_update_multiple_keys_in_one_call(tmp_path):
    c, store = _client_with_real_store(FakeFetch({}), tmp_path)
    with c:
        r = c.post("/api/strategy_params",
                   json={"trail_pct": 0.08, "volume_confirm_threshold": 2.0})
        assert r.status_code == 200
        body = c.get("/api/strategy_params").json()
        assert body["params"]["trail_pct"]["value"] == 0.08
        assert body["params"]["volume_confirm_threshold"]["value"] == 2.0


def test_root_page_displays_current_strategy_params_read_only(tmp_path):
    c, store = _client_with_real_store(FakeFetch({"AEHL": [_bars(3)]}), tmp_path)
    with c:
        page = c.get("/").text
        assert "id=\"strategy-params\"" in page or "id='strategy-params'" in page
        assert "trail_pct=0.0500" in page


def test_apply_bar_push_reaches_state_the_instant_its_awaited():
    # The whole point of push over poll: no sleep/wait needed to observe a
    # pushed bar land in Poller state -- see specs.md for the ~9s of
    # stacked polling latency this replaces. Driven directly on a
    # standalone Poller (no TestClient/HTTP layer) so the assertion can
    # run in the same coroutine, immediately after the await returns.
    from app import Poller

    async def run():
        poller = Poller(fetch_bars=FakeFetch({}), watch_symbol=None,
                        announce_watch=None)
        await poller.add_symbol("AEHL")
        bar = _bars(1, start=RTH, base=50.0)[0]

        await poller.apply_bar_push("AEHL", bar)  # no sleep before checking below

        st = poller.state_for("AEHL")
        assert st["bar_count"] == 1
        assert st["last_price"] == round(bar["close"], 4)

    asyncio.run(run())


def test_apply_bar_push_broadcasts_to_state_subscribers_synchronously():
    from app import Poller

    async def run():
        poller = Poller(fetch_bars=FakeFetch({}), watch_symbol=None,
                        announce_watch=None)
        await poller.add_symbol("AEHL")
        bar = _bars(1, start=RTH, base=50.0)[0]

        async with poller.subscribe_state() as q:
            await poller.apply_bar_push("AEHL", bar)
            payload = q.get_nowait()  # no await, no sleep
            assert payload["symbols"]["AEHL"]["bar_count"] == 1

    asyncio.run(run())


async def _drive_streaming_route(app, path):
    """Drive an ASGI streaming route directly, bypassing
    fastapi.testclient's httpx ASGITransport -- which fully buffers a
    response (runs the whole ASGI app call to completion) before
    returning anything to the caller, so it can never observe partial
    output from a route that streams until client disconnect, which is
    exactly what /api/state/stream (and schwab-connector's /events) do.
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


def test_state_stream_route_pushes_a_snapshot_then_a_live_update():
    fetch = FakeFetch({"AEHL": [_bars(3)]})
    app = create_app(fetch_bars=fetch, watch_symbol="AEHL", announce_watch=None)

    async def run():
        # Let the startup background task's initial catch_up land first,
        # so the immediate on-connect snapshot already shows AEHL.
        async def _lifespan_started():
            async with app.router.lifespan_context(app):
                task, chunks, disconnect = await _drive_streaming_route(app, "/api/state/stream")
                try:
                    first = await asyncio.wait_for(chunks.get(), timeout=2.0)
                    snapshot = json.loads(first.decode()[len("data: "):].strip())
                    assert "AEHL" in snapshot["symbols"] or snapshot["symbols"] == {}

                    await app.state.poller.apply_bar_push(
                        "AEHL", _bars(1, start=RTH + 1000, base=77.0)[0])
                    buf = b""
                    while b"data:" not in buf:
                        buf += await asyncio.wait_for(chunks.get(), timeout=2.0)
                    payload = json.loads(buf.decode()[len("data:"):].strip())
                    assert payload["symbols"]["AEHL"]["last_price"] == 77.0
                finally:
                    disconnect.set()
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
        await _lifespan_started()

    asyncio.run(run())


# -- page rendering (Stage A interim) ---------------------------------------

def test_root_page_has_no_meta_refresh():
    with _client(FakeFetch({"AEHL": [_bars(5)]})) as c:
        assert 'http-equiv="refresh"' not in c.get("/").text.lower()


def test_root_page_subscribes_to_state_updates_via_eventsource_without_reloading():
    # Fixed 2026-09-17 (specs.md): the browser leg of poll -> push. No more
    # setInterval(...) call anywhere in the page -- EventSource carries
    # live updates now; fetch('/api/state') is kept only for the explicit
    # post-action refresh() (watch/unwatch/journal actions), not a timer.
    with _client(FakeFetch({"AEHL": [_bars(5)]})) as c:
        page = c.get("/").text
        assert "new EventSource(" in page
        assert "/api/state/stream" in page
        assert "pollTimer" not in page  # the old timer variable is gone entirely
        assert "fetch(" in page
        assert "/api/state" in page


def test_root_page_has_a_pause_polling_toggle():
    with _client(FakeFetch({"AEHL": [_bars(5)]})) as c:
        page = c.get("/").text
        assert "poll-toggle" in page
        assert "poll_enabled" in page


def test_root_page_shows_no_symbols_message_when_idle():
    with _client(FakeFetch({}), symbol=None) as c:
        assert "no symbols watched" in c.get("/").text.lower()


def test_root_page_renders_a_card_per_watched_symbol():
    fetch = FakeFetch({"AEHL": [_bars(5)], "S2": [_bars(5, base=20.0)]})
    with _client(fetch, symbol="AEHL") as c:
        c.post("/api/watch", data={"symbol": "S2"})
        assert _wait_until(lambda: all(
            (_sym_state(c, s) or {}).get("status") == "ok" for s in ("AEHL", "S2")
        ))
        page = c.get("/").text
        assert "AEHL" in page
        assert "S2" in page


def test_root_page_renders_closest_setup_and_chips_for_other_candidates():
    # round_number_reclaim is always computable (no gating condition, per
    # setup_types.py), so an "ok" symbol always has at least one setup
    # candidate -- "Closest setup" must never show the empty-state text.
    fetch = FakeFetch({"AEHL": [_bars(5)]})
    with _client(fetch, symbol="AEHL") as c:
        assert _wait_until(lambda: (_sym_state(c, "AEHL") or {}).get("status") == "ok")
        state = _sym_state(c, "AEHL")
        assert len(state["setups"]) >= 1

        page = c.get("/").text
        # "Closest setup: <label> @ ..." is the non-empty-state render;
        # the bare "Closest setup</h3><p class='muted'>none currently
        # watchable" empty-state markup is checked separately below.
        # (The literal empty-state STRING also appears unconditionally
        # inside the embedded JS mirror's source text -- see closestSetupHtml
        # in _SCRIPT -- so "not in page" alone would be a false positive.)
        assert "Closest setup: " in page
        assert "<p class='muted'>none currently watchable</p>" not in page
        if len(state["setups"]) > 1:
            assert "setup-chip" in page


def test_root_page_collapses_resistance_and_support_behind_chips():
    # The raw resistance/support tables must not render always-open --
    # they're collapsed behind the same setup-chip/setup-detail pattern
    # as the other setup types, cut down from phase 3.5's always-open
    # design. No "<h3>Resistance (nearest above) @ ..." heading form
    # should appear at all; a "Resistance (nearest above) @ ..." CHIP
    # button, immediately followed by a hidden detail div, should.
    fetch = FakeFetch({"AEHL": [_bars(5)]})
    with _client(fetch, symbol="AEHL") as c:
        assert _wait_until(lambda: (_sym_state(c, "AEHL") or {}).get("status") == "ok")
        page = c.get("/").text
        assert "<h3>Resistance (nearest above)" not in page
        assert "<h3>Support (nearest below)" not in page
        assert "class='setup-chip'>Resistance (nearest above)" in page or \
            "class='setup-chip' disabled>Resistance (nearest above)" in page
        assert "class='setup-chip'>Support (nearest below)" in page or \
            "class='setup-chip' disabled>Support (nearest below)" in page


def test_setup_and_level_chips_carry_a_data_key_for_expand_state_restore():
    # refresh()'s JS restores which chips were expanded across a poll's
    # full #symbols rebuild by matching this data-key -- without it a
    # freshly-rebuilt chip always starts hidden and expanded state is
    # silently lost every ~4s poll (found live 2026-09-17). Needs enough
    # bars for real resistance/support levels to actually be detected
    # (_bars(5) produces neither -- both render as the disabled,
    # keyless chip variant, which isn't what this test is checking).
    fetch = FakeFetch({"AEHL": [_bars(60)]})
    with _client(fetch, symbol="AEHL") as c:
        assert _wait_until(lambda: (_sym_state(c, "AEHL") or {}).get("status") == "ok")
        state = _sym_state(c, "AEHL")
        assert state["levels"]["resistance"] is not None
        assert state["levels"]["support"] is not None
        assert len(state["setups"]) > 1

        page = c.get("/").text
        assert "data-key='AEHL:level:resistance'" in page
        assert "data-key='AEHL:level:support'" in page
        for setup in state["setups"][1:]:
            assert f"data-key='AEHL:setup:{setup['setup_type']}'" in page


# -- closed-trades table: symbol_switched rows visually muted ------------

def _closed_row(symbol, exit_reason, pnl=1.0, trade_id=1,
               entry_ts=1756909800, exit_ts=1756910400):
    # entry_ts=1756909800 -> 2025-09-03 10:30:00 ET; exit_ts=1756910400
    # (600s later) -> 2025-09-03 10:40:00 ET.
    return {"id": trade_id, "symbol": symbol, "entry_price": 10.0, "exit_price": 10.1,
            "exit_reason": exit_reason, "realized_pnl_pct": pnl,
            "entry_ts": entry_ts, "exit_ts": exit_ts}


def test_closed_row_shows_entry_and_exit_time_human_readable_not_epoch():
    rows = _journal_closed_rows_html([_closed_row("AEHL", "trailing_stop")])
    assert "1756909800" not in rows   # raw epoch never shown
    assert "1756910400" not in rows
    assert "10:30:00" in rows          # entry, America/New_York
    assert "10:40:00" in rows          # exit, America/New_York


def test_symbol_switched_rows_get_the_muted_housekeeping_class():
    rows = _journal_closed_rows_html([_closed_row("AEHL", "symbol_switched", pnl=2.5)])
    assert "<tr class='row-housekeeping'>" in rows
    # muted overrides the pos/neg P&L coloring a real trade outcome gets --
    # a symbol_switched exit isn't a strategy signal, so it must never be
    # colored as a win even though realized_pnl_pct is technically positive.
    assert "<td class='muted'>2.50%</td>" in rows
    assert "<td class='pos'>" not in rows


def test_trailing_stop_rows_are_not_muted_and_keep_pos_neg_coloring():
    rows = _journal_closed_rows_html([
        _closed_row("AEHL", "trailing_stop", pnl=2.5),
        _closed_row("MSFT", "trailing_stop", pnl=-1.0),
    ])
    assert "row-housekeeping" not in rows
    assert "<td class='pos'>2.50%</td>" in rows
    assert "<td class='neg'>-1.00%</td>" in rows


def test_closed_row_has_a_delete_button_scoped_to_its_own_trade_id():
    rows = _journal_closed_rows_html([_closed_row("AEHL", "trailing_stop", trade_id=42)])
    assert "class='remove-btn journal-delete-btn' data-trade-id='42'" in rows


def test_root_page_has_a_bulk_clear_symbol_switched_button():
    with _client(FakeFetch({"AEHL": [_bars(3)]})) as c:
        page = c.get("/").text
        assert "id=\"clear-symbol-switched-btn\"" in page
        # A 0-row delete is a real, correct success (nothing to clear) --
        # found live 2026-09-17: with no visible feedback at all, that
        # boring-but-correct success looked identical to the button
        # silently failing. This status span is what the click handler
        # reports "cleared N rows" / "nothing to clear" into.
        assert "id=\"clear-status\"" in page
