import asyncio
import json
import os
import sys
import threading
import time

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _APP_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(_APP_DIR), "core"))

import pytest
from fastapi.testclient import TestClient

from app import (
    _breakdown_setups_html,
    _closest_setup_html,
    _journal_closed_rows_html,
    _journal_open_html,
    _market_backdrop_html,
    create_app,
)

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
            trail_pct=0.05, max_symbols=4, stream_events=None,
            fetch_daily_bars=None, fetch_market_backdrop=None,
            market_backdrop_symbol="SPY", market_backdrop_refresh_seconds=180.0):
    app = create_app(fetch_bars=fetch, watch_symbol=symbol,
                     announce_watch=announce,
                     announce_unwatch=unwatch,
                     announce_retry_attempts=announce_retry_attempts,
                     announce_retry_base_delay=announce_retry_base_delay,
                     announce_retry_max_delay=announce_retry_max_delay,
                     journal_store=journal_store, trail_pct=trail_pct,
                     max_symbols=max_symbols, stream_events=stream_events,
                     fetch_daily_bars=fetch_daily_bars,
                     fetch_market_backdrop=fetch_market_backdrop,
                     market_backdrop_symbol=market_backdrop_symbol,
                     market_backdrop_refresh_seconds=market_backdrop_refresh_seconds)
    return TestClient(app)


def _sym_state(client, symbol):
    return client.get("/api/state").json()["symbols"].get(symbol)


# -- /api/state shape ------------------------------------------------------

def test_api_state_shape_has_symbols_recent_closed_poll_enabled_max_symbols():
    with _client(FakeFetch({"AEHL": [_bars(3)]})) as c:
        body = c.get("/api/state").json()
        assert set(body) == {"symbols", "recent_closed", "poll_enabled",
                             "max_symbols", "strategy_params", "current_equity",
                             "market_backdrop"}
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


# -- catalyst/context note at watch-time, specs.md section 7 --------------

def _client_with_real_store_for_notes(fetch, tmp_path, *, symbol="AEHL"):
    from journal_store import JournalStore
    store = JournalStore(tmp_path / "journal.db")
    return _client(fetch, symbol=symbol, journal_store=store), store


def test_post_watch_with_a_note_stores_and_displays_it(tmp_path):
    c, store = _client_with_real_store_for_notes(FakeFetch({"AEHL": [_bars(3)]}), tmp_path, symbol=None)
    with c:
        r = c.post("/api/watch", data={"symbol": "AEHL",
                                       "note": "halted on FDA news, watching for reclaim"})
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert store.current_note_for("AEHL") == "halted on FDA news, watching for reclaim"

        state = _sym_state(c, "AEHL") or {}
        assert state.get("watch_note") == "halted on FDA news, watching for reclaim"

        page = c.get("/").text
        assert "halted on FDA news, watching for reclaim" in page


def test_post_watch_without_a_note_is_valid_and_does_not_block(tmp_path):
    c, store = _client_with_real_store_for_notes(FakeFetch({"AEHL": [_bars(3)]}), tmp_path, symbol=None)
    with c:
        r = c.post("/api/watch", data={"symbol": "AEHL"})  # note omitted entirely
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert store.current_note_for("AEHL") is None

        page = c.get("/").text
        assert "no note recorded" in page


def test_post_watch_rejects_an_over_length_note(tmp_path):
    c, store = _client_with_real_store_for_notes(FakeFetch({}), tmp_path, symbol=None)
    with c:
        r = c.post("/api/watch", data={"symbol": "AEHL", "note": "x" * 501})
        assert r.status_code == 409
        assert r.json()["ok"] is False
        # rejected cleanly, not silently truncated -- AND the symbol
        # itself was never added, since the note was invalid
        assert store.current_note_for("AEHL") is None
        assert "AEHL" not in c.get("/api/state").json()["symbols"]


def test_post_watch_note_updates_without_disturbing_watch_state(tmp_path):
    c, store = _client_with_real_store_for_notes(FakeFetch({"AEHL": [_bars(3)]}), tmp_path)
    with c:
        assert _wait_until(lambda: (_sym_state(c, "AEHL") or {}).get("status") == "ok")
        r = c.post("/api/watch_note", json={"symbol": "AEHL",
                                            "note": "context added a minute later"})
        assert r.status_code == 200
        assert r.json()["ok"] is True

        # still watched, still has its real bars -- untouched
        assert (_sym_state(c, "AEHL") or {}).get("status") == "ok"
        assert (_sym_state(c, "AEHL") or {}).get("bar_count") == 3
        assert store.current_note_for("AEHL") == "context added a minute later"


def test_post_watch_note_rejects_an_unwatched_symbol(tmp_path):
    c, store = _client_with_real_store_for_notes(FakeFetch({}), tmp_path, symbol=None)
    with c:
        r = c.post("/api/watch_note", json={"symbol": "MSFT", "note": "anything"})
        assert r.status_code == 409
        assert r.json()["ok"] is False
        assert store.current_note_for("MSFT") is None


def test_post_watch_note_rejects_an_over_length_note(tmp_path):
    c, store = _client_with_real_store_for_notes(FakeFetch({"AEHL": [_bars(3)]}), tmp_path)
    with c:
        assert _wait_until(lambda: (_sym_state(c, "AEHL") or {}).get("status") == "ok")
        r = c.post("/api/watch_note", json={"symbol": "AEHL", "note": "x" * 501})
        assert r.status_code == 409
        assert r.json()["ok"] is False
        assert store.current_note_for("AEHL") is None


# -- reverse-split history flag, specs.md section 7 -----------------------

def test_reverse_splits_is_empty_list_when_none_recorded(tmp_path):
    c, store = _client_with_real_store_for_notes(FakeFetch({"AEHL": [_bars(3)]}), tmp_path)
    with c:
        assert _wait_until(lambda: (_sym_state(c, "AEHL") or {}).get("status") == "ok")
        assert _sym_state(c, "AEHL")["reverse_splits"] == []


def test_post_reverse_split_then_state_and_page_reflect_it(tmp_path):
    c, store = _client_with_real_store_for_notes(FakeFetch({"AEHL": [_bars(3)]}), tmp_path)
    with c:
        assert _wait_until(lambda: (_sym_state(c, "AEHL") or {}).get("status") == "ok")
        r = c.post("/api/reverse_splits", json={
            "symbol": "AEHL", "split_date": "2024-05-02", "ratio": "1:10",
            "note": "pre-earnings reverse split"})
        assert r.status_code == 200
        assert r.json()["ok"] is True

        splits = _sym_state(c, "AEHL")["reverse_splits"]
        assert len(splits) == 1
        assert splits[0]["ratio"] == "1:10"

        page = c.get("/").text
        assert "1:10" in page
        assert "2024-05-02" in page


def test_reverse_split_flag_not_shown_when_none_recorded(tmp_path):
    c, store = _client_with_real_store_for_notes(FakeFetch({"AEHL": [_bars(3)]}), tmp_path)
    with c:
        assert _wait_until(lambda: (_sym_state(c, "AEHL") or {}).get("status") == "ok")
        page = c.get("/").text
        # Only the server-rendered markup matters here -- the trailing
        # <script> block's JS mirror (reverseSplitsHtml) legitimately
        # contains this string as a template literal regardless of state.
        rendered = page.split("<script>")[0]
        assert "Reverse-split history" not in rendered


def test_post_reverse_split_works_for_a_symbol_not_currently_watched(tmp_path):
    # The whole point of the flag: checkable BEFORE deciding to watch a
    # symbol, not only after.
    c, store = _client_with_real_store_for_notes(FakeFetch({}), tmp_path, symbol=None)
    with c:
        r = c.post("/api/reverse_splits", json={
            "symbol": "BIAF", "split_date": "2024-05-02", "ratio": "1:10"})
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert store.reverse_splits_for("BIAF")[0]["ratio"] == "1:10"


def test_get_reverse_splits_returns_recorded_splits(tmp_path):
    c, store = _client_with_real_store_for_notes(FakeFetch({}), tmp_path, symbol=None)
    with c:
        store.add_reverse_split("BIAF", "2024-05-02", "1:10")
        r = c.get("/api/reverse_splits", params={"symbol": "BIAF"})
        assert r.status_code == 200
        body = r.json()
        assert body["symbol"] == "BIAF"
        assert len(body["splits"]) == 1
        assert body["splits"][0]["ratio"] == "1:10"


def test_post_reverse_split_rejects_a_non_iso_split_date(tmp_path):
    c, store = _client_with_real_store_for_notes(FakeFetch({}), tmp_path, symbol=None)
    with c:
        r = c.post("/api/reverse_splits", json={
            "symbol": "BIAF", "split_date": "05/02/2024", "ratio": "1:10"})
        assert r.status_code == 409
        assert r.json()["ok"] is False
        assert store.reverse_splits_for("BIAF") == []


def test_post_reverse_split_rejects_a_blank_ratio(tmp_path):
    c, store = _client_with_real_store_for_notes(FakeFetch({}), tmp_path, symbol=None)
    with c:
        r = c.post("/api/reverse_splits", json={
            "symbol": "BIAF", "split_date": "2024-05-02", "ratio": ""})
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

        def current_note_for(self, symbol):
            return None

        def reverse_splits_for(self, symbol):
            return []

        def current_equity(self):
            return 2000.0

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


# -- position sizing with compounding virtual equity, specs.md section 7 --

def test_get_equity_reports_current_value_and_empty_history_initially(tmp_path):
    c, store = _client_with_real_store(FakeFetch({}), tmp_path)
    with c:
        body = c.get("/api/equity").json()
        assert body["current_equity"] == 2000.0
        assert body["history"] == []


def test_post_equity_reset_sets_to_live_base_equity_param(tmp_path):
    c, store = _client_with_real_store(
        FakeFetch({}), tmp_path,
        default_params={"trail_pct": 0.05, "volume_confirm_threshold": 1.5,
                        "base_equity": 2000.0, "risk_pct_per_trade": 0.01})
    with c:
        store.apply_realized_pnl(trade_id=1, pnl_dollars=250.0)
        assert c.get("/api/equity").json()["current_equity"] == 2250.0

        c.post("/api/strategy_params", json={"base_equity": 5000.0})
        r = c.post("/api/equity/reset")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert r.json()["current_equity"] == 5000.0

        body = c.get("/api/equity").json()
        assert body["current_equity"] == 5000.0
        assert body["history"][0]["reason"] == "manual_reset"


def test_post_equity_override_sets_an_arbitrary_value(tmp_path):
    c, store = _client_with_real_store(FakeFetch({}), tmp_path)
    with c:
        r = c.post("/api/equity/override", json={"value": 777.0})
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert c.get("/api/equity").json()["current_equity"] == 777.0


def test_post_equity_override_rejects_a_non_positive_value(tmp_path):
    c, store = _client_with_real_store(FakeFetch({}), tmp_path)
    with c:
        r = c.post("/api/equity/override", json={"value": 0.0})
        assert r.status_code == 409
        assert r.json()["ok"] is False
        assert c.get("/api/equity").json()["current_equity"] == 2000.0


def test_root_page_displays_current_equity(tmp_path):
    c, store = _client_with_real_store(FakeFetch({"AEHL": [_bars(3)]}), tmp_path)
    with c:
        page = c.get("/").text
        assert "id=\"current-equity\"" in page or "id='current-equity'" in page
        assert "$2,000.00" in page


def test_root_page_displays_base_equity_and_risk_pct_alongside_strategy_params(tmp_path):
    c, store = _client_with_real_store(
        FakeFetch({"AEHL": [_bars(3)]}), tmp_path,
        default_params={"trail_pct": 0.05, "volume_confirm_threshold": 1.5,
                        "base_equity": 2000.0, "risk_pct_per_trade": 0.01})
    with c:
        page = c.get("/").text
        assert "base_equity=2000.0000" in page
        assert "risk_pct_per_trade=0.0100" in page


# -- two-phase exit + session-level volume gate, specs.md section 12 ------

def test_root_page_displays_the_new_section_13_strategy_params(tmp_path):
    c, store = _client_with_real_store(
        FakeFetch({"AEHL": [_bars(3)]}), tmp_path,
        default_params={"trail_pct": 0.05, "volume_confirm_threshold": 1.5,
                        "swing_low_buffer_pct": 0.005,
                        "pattern_progress_threshold_pct": 0.03,
                        "session_volume_multiple": 3.0})
    with c:
        page = c.get("/").text
        assert "swing_low_buffer_pct=0.0050" in page
        assert "pattern_progress_threshold_pct=0.0300" in page
        assert "session_volume_multiple=3.0000" in page


def test_avg_daily_volume_is_none_when_no_fetcher_configured(tmp_path):
    c, store = _client_with_real_store(FakeFetch({"AEHL": [_bars(3)]}), tmp_path)
    with c:
        assert _wait_until(lambda: (_sym_state(c, "AEHL") or {}).get("status") == "ok")
        assert _sym_state(c, "AEHL")["avg_daily_volume"] is None


def test_add_symbol_fetches_daily_bars_once_and_caches_the_average(tmp_path):
    calls = []

    async def fetch_daily_bars(symbol):
        calls.append(symbol)
        return [{"ts": i, "volume": v, "open": 1, "high": 1, "low": 1,
                 "close": 1, "is_extended": False}
                for i, v in enumerate([100_000.0, 200_000.0, 300_000.0])]

    c = _client(FakeFetch({"AEHL": [_bars(3)]}), symbol="AEHL",
               fetch_daily_bars=fetch_daily_bars)
    with c:
        assert _wait_until(lambda: (_sym_state(c, "AEHL") or {}).get("status") == "ok")
        assert calls == ["AEHL"]
        assert _sym_state(c, "AEHL")["avg_daily_volume"] == 200_000.0

        # Never re-fetched on ordinary bar pushes/resyncs, only at add-time.
        c.post("/api/polling", json={"enabled": False})
        c.post("/api/polling", json={"enabled": True})
        assert calls == ["AEHL"]


# -- continuation-vs-fresh-day flag, specs.md section 7 -- REUSES the
# same daily-bars fetch built for the volume gate above, never a second
# pull of the same underlying data.

def _daily_bar(ts, close, vol=100_000.0):
    return {"ts": ts, "volume": vol, "open": close, "high": close,
           "low": close, "close": close, "is_extended": False}


def test_continuation_flag_reuses_the_single_daily_bars_fetch_not_a_second_one(tmp_path):
    calls = []
    closes = [10.0, 10.1, 9.9, 21.0, 20.5, 20.0, 19.8, 19.9]  # a real +103% day

    async def fetch_daily_bars(symbol):
        calls.append(symbol)
        return [_daily_bar(i, c) for i, c in enumerate(closes)]

    c = _client(FakeFetch({"AEHL": [_bars(3)]}), symbol="AEHL",
               fetch_daily_bars=fetch_daily_bars)
    with c:
        assert _wait_until(lambda: (_sym_state(c, "AEHL") or {}).get("status") == "ok")
        state = _sym_state(c, "AEHL")
        # BOTH avg_daily_volume and the continuation flag are populated...
        assert state["avg_daily_volume"] == 100_000.0
        assert state["continuation"]["status"] == "continuation"
        # ...from exactly ONE fetch call -- the real assertion this test
        # exists for, not just "it works."
        assert calls == ["AEHL"]

        c.post("/api/polling", json={"enabled": False})
        c.post("/api/polling", json={"enabled": True})
        assert calls == ["AEHL"]


def test_continuation_flag_identifies_a_real_continuation_day():
    closes = [10.0, 10.1, 9.9, 21.0, 20.5, 20.0, 19.8, 19.9]

    async def fetch_daily_bars(symbol):
        return [_daily_bar(i, c) for i, c in enumerate(closes)]

    c = _client(FakeFetch({"AEHL": [_bars(3)]}), symbol="AEHL",
               fetch_daily_bars=fetch_daily_bars)
    with c:
        assert _wait_until(lambda: (_sym_state(c, "AEHL") or {}).get("status") == "ok")
        continuation = _sym_state(c, "AEHL")["continuation"]
        assert continuation["status"] == "continuation"
        assert len(continuation["days"]) == 1
        assert continuation["days"][0]["ts"] == 3
        assert continuation["days"][0]["pct_change"] == pytest.approx((21.0 - 9.9) / 9.9)


def test_continuation_flag_identifies_a_genuinely_fresh_symbol():
    closes = [10.0, 10.2, 9.9, 10.1, 10.0, 9.8, 10.05, 10.1]  # ordinary noise

    async def fetch_daily_bars(symbol):
        return [_daily_bar(i, c) for i, c in enumerate(closes)]

    c = _client(FakeFetch({"AEHL": [_bars(3)]}), symbol="AEHL",
               fetch_daily_bars=fetch_daily_bars)
    with c:
        assert _wait_until(lambda: (_sym_state(c, "AEHL") or {}).get("status") == "ok")
        continuation = _sym_state(c, "AEHL")["continuation"]
        assert continuation["status"] == "fresh"
        assert continuation["days"] == []
        assert continuation["lookback_days_used"] == 7
        assert continuation["threshold_pct_used"] == 0.5


def test_continuation_flag_is_unknown_without_a_configured_fetcher(tmp_path):
    c = _client(FakeFetch({"AEHL": [_bars(3)]}), symbol="AEHL")  # no fetch_daily_bars
    with c:
        assert _wait_until(lambda: (_sym_state(c, "AEHL") or {}).get("status") == "ok")
        continuation = _sym_state(c, "AEHL")["continuation"]
        assert continuation["status"] == "unknown"
        assert continuation["days"] == []


def test_root_page_always_shows_continuation_status_fresh_or_flagged():
    fresh_closes = [10.0, 10.2, 9.9, 10.1, 10.0, 9.8, 10.05, 10.1]

    async def fetch_daily_bars(symbol):
        return [_daily_bar(i, c) for i, c in enumerate(fresh_closes)]

    c = _client(FakeFetch({"AEHL": [_bars(3)]}), symbol="AEHL",
               fetch_daily_bars=fetch_daily_bars)
    with c:
        assert _wait_until(lambda: (_sym_state(c, "AEHL") or {}).get("status") == "ok")
        page = c.get("/").text
        rendered = page.split("<script>")[0]
        assert "Day 1 (fresh)" in rendered


def test_root_page_shows_continuation_flag_with_real_date_and_magnitude():
    closes = [10.0, 10.1, 9.9, 21.0, 20.5, 20.0, 19.8, 19.9]

    async def fetch_daily_bars(symbol):
        return [_daily_bar(i, c) for i, c in enumerate(closes)]

    c = _client(FakeFetch({"AEHL": [_bars(3)]}), symbol="AEHL",
               fetch_daily_bars=fetch_daily_bars)
    with c:
        assert _wait_until(lambda: (_sym_state(c, "AEHL") or {}).get("status") == "ok")
        page = c.get("/").text
        rendered = page.split("<script>")[0]
        assert "Continuation" in rendered
        assert "+112." in rendered  # (21.0-9.9)/9.9 = +112.1%


def test_add_symbol_leaves_avg_daily_volume_none_when_fetch_fails(tmp_path):
    async def failing_fetch_daily_bars(symbol):
        raise RuntimeError("companion-auth unreachable")

    c = _client(FakeFetch({"AEHL": [_bars(3)]}), symbol="AEHL",
               fetch_daily_bars=failing_fetch_daily_bars)
    with c:
        # The add itself must not fail/block just because the daily-
        # history fetch did.
        assert _wait_until(lambda: (_sym_state(c, "AEHL") or {}).get("status") == "ok")
        assert _sym_state(c, "AEHL")["avg_daily_volume"] is None


def test_add_symbol_leaves_avg_daily_volume_none_when_fetch_returns_empty(tmp_path):
    async def empty_fetch_daily_bars(symbol):
        return []

    c = _client(FakeFetch({"AEHL": [_bars(3)]}), symbol="AEHL",
               fetch_daily_bars=empty_fetch_daily_bars)
    with c:
        assert _wait_until(lambda: (_sym_state(c, "AEHL") or {}).get("status") == "ok")
        assert _sym_state(c, "AEHL")["avg_daily_volume"] is None


def test_open_position_panel_shows_the_current_stop_phase():
    open_block = {"symbol": "AEHL", "entry_price": 10.0, "stop_level": 9.5,
                 "unrealized_pnl_pct": 0.0, "shares": 10,
                 "unrealized_pnl_dollars": 0.0, "exit_phase": "swing_low"}
    html_out = _journal_open_html(open_block)
    assert "swing-low anchored" in html_out

    open_block["exit_phase"] = "trailing"
    html_out = _journal_open_html(open_block)
    assert "flat trailing" in html_out


# -- reference-target display, informational only (specs.md section 21) --
# explicitly does NOT change exit logic -- see test_journal_logic.py /
# test_journal_wiring.py, unmodified by this feature, for should_enter/
# advance_journal's own unaffected test coverage.

def _open_block(**overrides):
    base = {"symbol": "AEHL", "entry_price": 10.0, "stop_level": 9.5,
           "unrealized_pnl_pct": 0.0, "shares": 10,
           "unrealized_pnl_dollars": 0.0, "exit_phase": "swing_low",
           "nearest_resistance_above": 11.5,
           "target_reference_pct": 0.10, "target_reference_price": 11.0}
    base.update(overrides)
    return base


def test_open_position_panel_shows_nearest_resistance_reference():
    html_out = _journal_open_html(_open_block(nearest_resistance_above=11.5))
    assert "reference only" in html_out
    assert "11.5" in html_out


def test_open_position_panel_shows_none_when_no_resistance_above():
    # Matches the existing "none on this side of price" language the
    # levels table elsewhere on the page already uses for this same case
    # (_level_block_html) -- not a new, inconsistent phrase.
    html_out = _journal_open_html(_open_block(nearest_resistance_above=None))
    assert "none on this side of price" in html_out


def test_open_position_panel_shows_target_reference_price():
    html_out = _journal_open_html(_open_block(target_reference_price=11.0, target_reference_pct=0.10))
    assert "11.0" in html_out
    assert "+10%" in html_out
    assert "reference only" in html_out


def test_open_position_panel_reference_rows_survive_a_hand_built_block_missing_the_new_fields():
    # Backward compatibility: an open_block from before this feature
    # (missing both new keys entirely) must still render, not KeyError --
    # .get(), not direct indexing.
    open_block = {"symbol": "AEHL", "entry_price": 10.0, "stop_level": 9.5,
                 "unrealized_pnl_pct": 0.0, "shares": 10,
                 "unrealized_pnl_dollars": 0.0, "exit_phase": "swing_low"}
    html_out = _journal_open_html(open_block)
    assert "none on this side of price" in html_out
    assert "—" in html_out  # em-dash fallback for a missing target price


# -- breakdown-below setup variants + closest-setup wording (specs.md ------
# section 22). Breakdown types are warning/context signals only, never a
# trade trigger (see test_journal_logic.py's structural safety proof) --
# these tests are purely about the DISPLAY: a separate, clearly-labeled
# section, distinct from the bullish closest-setup callout, and that
# callout's own wording changing when a position is already open.

def _setup_dict(setup_type="resistance_breakout", trigger_price=10.5,
                distance=1.0, confirmed=True, **factors):
    return {
        "setup_type": setup_type,
        "trigger_price": trigger_price,
        "distance": distance,
        "hold": {"direction": "above", "required_seconds": 30.0,
                 "elapsed_seconds": 30.0, "confirmed": confirmed,
                 "failed_attempts": 0, "confirmed_at_ts": 100.0},
        "factors": factors or {"strength_score": 5.0, "touch_count": 2},
    }


def test_closest_setup_html_reads_normally_when_no_position_open():
    html_out = _closest_setup_html(_setup_dict(), position_open=False)
    assert "Closest setup: " in html_out
    assert "position already open" not in html_out


def test_closest_setup_html_changes_wording_when_position_is_open():
    html_out = _closest_setup_html(_setup_dict(), position_open=True)
    assert "Setup context (position already open, not a new signal)" in html_out
    assert "<h3>Closest setup: " not in html_out
    # the underlying setup data is still shown, just relabeled -- the
    # user explicitly asked for context to stay visible, not suppressed.
    assert "resistance breakout" in html_out.lower() or "Resistance breakout" in html_out


def test_closest_setup_html_position_open_default_is_false():
    # Existing callers (pre-this-feature) that don't pass position_open
    # at all must keep rendering exactly as before.
    html_out = _closest_setup_html(_setup_dict())
    assert "Closest setup: " in html_out


def test_closest_setup_html_empty_state_unaffected_by_position_open():
    assert _closest_setup_html(None, position_open=True) == \
        _closest_setup_html(None, position_open=False)


def test_breakdown_setups_html_empty_when_none_present():
    assert _breakdown_setups_html("AEHL", []) == ""


def test_breakdown_setups_html_renders_a_clearly_labeled_separate_section():
    setups = [_setup_dict("support_breakdown", trigger_price=9.0, distance=0.5,
                          strength_score=6.0, touch_count=2)]
    html_out = _breakdown_setups_html("AEHL", setups)
    assert "Bearish signals" in html_out
    assert "context only" in html_out
    assert "not a trade opportunity" in html_out
    assert "Support breakdown" in html_out
    assert "breakdown-setups" in html_out  # its own section, not setup-chips alone


def test_breakdown_setups_html_never_uses_the_closest_setup_wording():
    # Must not be confusable with the bullish closest-setup callout --
    # no "Closest setup" framing anywhere in this section's own output.
    setups = [_setup_dict("round_number_breakdown", requires_prior_touches=False)]
    html_out = _breakdown_setups_html("AEHL", setups)
    assert "Closest setup" not in html_out
    assert "Setup context" not in html_out


def test_breakdown_setups_html_renders_all_four_types_with_distinct_labels():
    types = ["support_breakdown", "micro_breakdown", "vwap_breakdown", "round_number_breakdown"]
    setups = [_setup_dict(t) for t in types]
    html_out = _breakdown_setups_html("AEHL", setups)
    for expected_label in ("Support breakdown", "Micro-breakdown",
                          "VWAP breakdown", "Round-number breakdown"):
        assert expected_label in html_out


def test_root_page_shows_bearish_signals_section_for_a_real_breakdown_scenario():
    # Real, live-computed data (not a hand-built factors dict):
    # round_number_breakdown is always watchable regardless of trend (same
    # "always present" nature as its bullish mirror, round_number_reclaim
    # -- see core/setup_types.py's _round_number_breakdown_candidate), so
    # this proves the section actually renders end-to-end through
    # build_state -> the real page, not just the unit-level renderer above.
    fetch = FakeFetch({"AEHL": [_bars(30)]})
    with _client(fetch, symbol="AEHL") as c:
        assert _wait_until(lambda: (_sym_state(c, "AEHL") or {}).get("status") == "ok")
        state = _sym_state(c, "AEHL")
        assert len(state["breakdown_setups"]) >= 1
        page = c.get("/").text
        assert "Bearish signals" in page
        assert "breakdown-chip" in page


def _double_top_bars(start=RTH, step=60):
    """Two real swing highs (~8.69), far enough apart to be distinct
    swing points, close enough to cluster into one resistance level --
    the same known-good shape core/tests/test_setup_types.py's
    _double_top_bars uses, reused here (not re-derived) so this is
    checked against a level detect_levels is already proven to find."""
    bars, ts = [], start
    for p in [7.0, 7.5, 8.2, 8.69, 8.0, 7.6, 7.0, 6.9, 7.1,
             7.4, 7.9, 8.3, 8.65, 7.9, 7.6, 7.5, 7.4, 7.3]:
        bars.append({"ts": ts, "open": p, "high": p + 0.05, "low": p - 0.05,
                    "close": p, "volume": 50_000.0, "is_extended": False})
        ts += step
    return bars


def test_journal_open_for_nearest_resistance_is_live_not_locked_at_entry():
    # Wired to the SAME slot.state["levels"]["resistance"] the levels
    # table elsewhere on the page already shows -- proven here by
    # equality against that real, independently-computed value, not a
    # hardcoded expected price (build_state's own detect_levels
    # correctness is already proven extensively elsewhere). Then proves
    # it's genuinely LIVE: pushing more bars that change the real
    # detected resistance changes this reference too, without a new
    # entry -- unlike trail_pct_used and the position's other real
    # "used" snapshots.
    from app import Poller
    from journal_logic import OpenPosition

    async def run():
        poller = Poller(fetch_bars=FakeFetch({}), watch_symbol=None,
                        announce_watch=None, target_reference_pct=0.10)
        await poller.add_symbol("AEHL")
        for bar in _double_top_bars():
            await poller.apply_bar_push("AEHL", bar)

        slot = poller._slots["AEHL"]
        slot.journal_position = OpenPosition(
            id=1, symbol="AEHL", entry_ts=slot.bars[0]["ts"], entry_price=8.0,
            high_water_mark=8.0, stop_level=7.6,
        )

        real_resistance = slot.state["levels"]["resistance"]
        full = poller.full_state_for("AEHL")
        open_block = full["journal"]["open"]
        assert real_resistance is not None  # sanity: the double-top really was found
        assert open_block["nearest_resistance_above"] == real_resistance["price"]
        assert open_block["target_reference_price"] == round(8.0 * 1.10, 4)
        assert open_block["target_reference_pct"] == 0.10

        # Prove liveness directly: slot.state is rebuilt fresh on every
        # real poll (specs.md section 3 -- "full recompute keeps the app
        # trivially correct"), so if a LATER real recompute finds a
        # DIFFERENT resistance, _journal_open_for must reflect it without
        # a new entry -- unlike a snapshotted "used" field, which would
        # keep showing the OLD value regardless of what slot.state says
        # now. Mutating slot.state directly here (rather than fighting
        # detect_levels' real scoring to force a different top pick)
        # isolates exactly that: does this read slot.state live, every
        # call, or does it cache/snapshot anything.
        slot.state["levels"]["resistance"] = {**real_resistance, "price": real_resistance["price"] + 1.0}
        changed_open_block = poller.full_state_for("AEHL")["journal"]["open"]
        assert changed_open_block["nearest_resistance_above"] == round(real_resistance["price"] + 1.0, 4)

    asyncio.run(run())


def test_journal_open_for_shows_none_when_no_resistance_above_price():
    from app import Poller
    from journal_logic import OpenPosition

    async def run():
        poller = Poller(fetch_bars=FakeFetch({}), watch_symbol=None, announce_watch=None)
        await poller.add_symbol("AEHL")
        # Monotonically rising, flat bars -- no real swing high anywhere
        # above the current price for detect_levels to find.
        ts = RTH
        for p in [10.0, 10.0, 10.0, 10.0, 10.0]:
            await poller.apply_bar_push("AEHL", {
                "ts": ts, "open": p, "high": p, "low": p, "close": p,
                "volume": 1000.0, "is_extended": False,
            })
            ts += 60
        slot = poller._slots["AEHL"]
        slot.journal_position = OpenPosition(
            id=1, symbol="AEHL", entry_ts=slot.bars[0]["ts"], entry_price=10.0,
            high_water_mark=10.0, stop_level=9.5,
        )
        assert slot.state["levels"]["resistance"] is None  # sanity
        open_block = poller.full_state_for("AEHL")["journal"]["open"]
        assert open_block["nearest_resistance_above"] is None

    asyncio.run(run())


def test_journal_open_for_target_reference_reads_the_live_strategy_param(tmp_path):
    # Live-tunable, like every other strategy_param in this project --
    # changing it via the store (no new entry) changes the displayed
    # value immediately, computed from the position's real entry_price.
    from app import Poller
    from journal_logic import OpenPosition
    from journal_store import JournalStore

    async def run():
        store = JournalStore(tmp_path / "journal.db",
                             default_params={"target_reference_pct": 0.10})
        poller = Poller(fetch_bars=FakeFetch({}), watch_symbol=None,
                        announce_watch=None, journal_store=store,
                        target_reference_pct=0.10)
        await poller.add_symbol("AEHL")
        slot = poller._slots["AEHL"]
        slot.journal_position = OpenPosition(
            id=1, symbol="AEHL", entry_ts=0, entry_price=20.0,
            high_water_mark=20.0, stop_level=19.0,
        )
        open_block = poller.full_state_for("AEHL")["journal"]["open"]
        assert open_block["target_reference_price"] == round(20.0 * 1.10, 4)

        store.set_param("target_reference_pct", 0.25)
        updated_block = poller.full_state_for("AEHL")["journal"]["open"]
        assert updated_block["target_reference_price"] == round(20.0 * 1.25, 4)
        assert updated_block["entry_price"] == 20.0  # the position itself is unaffected

    asyncio.run(run())


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


# -- real dollar P&L / zero-size flag display, specs.md section 7 ----------

def test_closed_row_shows_real_dollar_pnl_alongside_percentage():
    row = _closed_row("AEHL", "trailing_stop", pnl=2.5)
    row["shares"] = 43
    row["realized_pnl_dollars"] = 19.565
    rows = _journal_closed_rows_html([row])
    assert "$19.57" in rows or "$19.56" in rows  # float rounding, either is correct
    assert ">43<" in rows


def test_closed_row_flags_a_zero_share_trade_distinctly_from_unset(tmp_path):
    zero_row = _closed_row("AEHL", "trailing_stop", trade_id=1)
    zero_row["shares"] = 0
    zero_row["realized_pnl_dollars"] = 0.0
    unset_row = _closed_row("AEHL", "trailing_stop", trade_id=2)
    unset_row["shares"] = None
    unset_row["realized_pnl_dollars"] = None
    rows = _journal_closed_rows_html([zero_row, unset_row])
    assert "zero-size-flag" in rows
    assert rows.count("zero-size-flag") == 1  # only the genuinely-zero row


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


# -- market backdrop, display only, specs.md section 23 --------------------
# Global context (SPY's own day change), never wired into should_enter/
# advance_journal/sizing. Reuses the daily-bars fetch mechanism (built for
# the session volume gate / continuation flag) pointed at SPY, refreshed
# on its own independent periodic cycle -- deliberately NOT a 5th
# streaming-subscribed slot.

def _spy_bars(prior_close=415.58, current_price=417.32, prior_ts=RTH - 86400, today_ts=RTH):
    return [
        {"ts": prior_ts, "open": 410.0, "high": 412.0, "low": 409.0,
         "close": prior_close, "volume": 50_000_000.0, "is_extended": False},
        {"ts": today_ts, "open": prior_close, "high": 418.0, "low": 415.0,
         "close": current_price, "volume": 20_000_000.0, "is_extended": False},
    ]


def test_market_backdrop_html_unknown_before_first_fetch():
    html_out = _market_backdrop_html({"status": "unknown", "symbol": "SPY"})
    assert "unknown" in html_out
    assert "market-backdrop" in html_out


def test_market_backdrop_html_shows_pct_change_and_prices_when_ok():
    backdrop = {"status": "ok", "symbol": "SPY", "current_price": 417.32,
               "prior_close": 415.58, "pct_change": 0.004187, "as_of_ts": RTH}
    html_out = _market_backdrop_html(backdrop)
    assert "SPY" in html_out
    assert "+0.42%" in html_out
    assert "417.32" in html_out
    assert "415.58" in html_out
    assert "pos" in html_out  # positive-day coloring


def test_market_backdrop_html_negative_day_uses_neg_class_and_sign():
    backdrop = {"status": "ok", "symbol": "SPY", "current_price": 410.0,
               "prior_close": 415.58, "pct_change": -0.01342, "as_of_ts": RTH}
    html_out = _market_backdrop_html(backdrop)
    assert "-1.34%" in html_out
    assert "neg" in html_out
    assert "+.34%" not in html_out  # no stray plus sign on a negative day


def test_root_page_renders_market_backdrop_once_at_the_page_level_not_per_panel():
    async def fetch_backdrop(symbol):
        return _spy_bars()

    fetch = FakeFetch({"AEHL": [_bars(3)], "S2": [_bars(3, base=20.0)]})
    with _client(fetch, symbol="AEHL", max_symbols=4,
                fetch_market_backdrop=fetch_backdrop) as c:
        c.post("/api/watch", data={"symbol": "S2"})
        assert _wait_until(lambda: all(
            (_sym_state(c, s) or {}).get("status") == "ok" for s in ("AEHL", "S2")
        ))
        assert _wait_until(lambda: c.get("/api/state").json()["market_backdrop"]["status"] == "ok")
        page = c.get("/").text
        # exactly one page-level occurrence, not one per symbol card.
        # Single-quoted id= is the Python-rendered server markup
        # specifically (_market_backdrop_html) -- the embedded JS
        # mirror's own source text contains the SAME id as a double-
        # quoted string literal unconditionally, which would be a false
        # positive here (same pitfall test_root_page_renders_closest_
        # setup_and_chips_for_other_candidates already documents).
        assert page.count("id='market-backdrop'") == 1
        assert "Market backdrop: SPY" in page


def test_market_backdrop_refreshes_on_its_own_periodic_cycle():
    calls = []

    async def fetch_backdrop(symbol):
        calls.append(symbol)
        return _spy_bars()

    with _client(FakeFetch({"AEHL": [_bars(3)]}), fetch_market_backdrop=fetch_backdrop,
                market_backdrop_symbol="SPY", market_backdrop_refresh_seconds=0.05) as c:
        assert _wait_until(lambda: len(calls) >= 3, timeout=2.0)
        assert all(sym == "SPY" for sym in calls)


def test_market_backdrop_refresh_is_independent_of_watched_symbol_polling():
    # No watched symbols at all -- the backdrop poll must still run, since
    # it's an entirely separate mechanism from the 4-symbol streaming
    # update path (specs.md section 23).
    backdrop_calls = []

    async def fetch_backdrop(symbol):
        backdrop_calls.append(symbol)
        return _spy_bars()

    with _client(FakeFetch({}), symbol=None, fetch_market_backdrop=fetch_backdrop,
                market_backdrop_refresh_seconds=0.05) as c:
        assert _wait_until(lambda: len(backdrop_calls) >= 2, timeout=2.0)
        assert c.get("/api/state").json()["symbols"] == {}


def test_market_backdrop_stays_unknown_when_not_configured():
    with _client(FakeFetch({"AEHL": [_bars(3)]})) as c:  # fetch_market_backdrop=None
        assert _wait_until(lambda: (_sym_state(c, "AEHL") or {}).get("status") == "ok")
        assert c.get("/api/state").json()["market_backdrop"]["status"] == "unknown"


def test_market_backdrop_stays_unknown_on_fetch_failure_not_a_crash():
    async def failing_fetch(symbol):
        raise RuntimeError("connector unreachable")

    with _client(FakeFetch({"AEHL": [_bars(3)]}), fetch_market_backdrop=failing_fetch,
                market_backdrop_refresh_seconds=0.05) as c:
        assert _wait_until(lambda: (_sym_state(c, "AEHL") or {}).get("status") == "ok")
        # gave the (failing) loop a couple cycles -- still up, still unknown
        time.sleep(0.15)
        body = c.get("/api/state").json()
        assert body["market_backdrop"]["status"] == "unknown"


def test_market_backdrop_unknown_when_fetch_returns_fewer_than_two_bars():
    async def one_bar_fetch(symbol):
        return _spy_bars()[-1:]

    with _client(FakeFetch({"AEHL": [_bars(3)]}), fetch_market_backdrop=one_bar_fetch,
                market_backdrop_refresh_seconds=0.05) as c:
        time.sleep(0.15)
        assert c.get("/api/state").json()["market_backdrop"]["status"] == "unknown"


def test_market_backdrop_never_influences_should_enter_or_advance_journal():
    # Structural, not conventional (same standard this project has proven
    # for every other display-only feature -- section 21's reference-
    # target, section 22's breakdown setups): neither function has ANY
    # market-backdrop-related parameter in its signature at all.
    import inspect
    from journal_logic import advance_journal, should_enter
    assert "market_backdrop" not in inspect.signature(should_enter).parameters
    assert "market_backdrop" not in inspect.signature(advance_journal).parameters


def test_market_backdrop_symbol_is_configurable():
    async def fetch_backdrop(symbol):
        assert symbol == "QQQ"
        return _spy_bars()

    with _client(FakeFetch({"AEHL": [_bars(3)]}), fetch_market_backdrop=fetch_backdrop,
                market_backdrop_symbol="QQQ", market_backdrop_refresh_seconds=0.05) as c:
        assert _wait_until(lambda: c.get("/api/state").json()["market_backdrop"]["symbol"] == "QQQ")
        assert _wait_until(lambda: c.get("/api/state").json()["market_backdrop"]["status"] == "ok")


# -- human review/labeling for closed trades, specs.md section 24 ---------
# The first place a human judgment gets attached to a trade after the
# fact, rather than something the system computed about its own
# mechanical decisions. Closed trades only -- an open position's outcome
# isn't known yet.

def test_closed_row_shows_not_reviewed_when_never_reviewed():
    rows = _journal_closed_rows_html([_closed_row("AEHL", "trailing_stop")])
    assert "not reviewed" in rows
    assert "journal-review-btn" in rows
    assert "data-trade-id='1'" in rows  # scoped to its own trade, like delete


def test_closed_row_shows_the_review_label_badge_when_reviewed():
    row = _closed_row("AEHL", "trailing_stop")
    row["review_label"] = "clean_signal"
    rows = _journal_closed_rows_html([row])
    assert "review-label-clean_signal" in rows
    assert ">clean_signal<" in rows


def test_closed_row_review_form_prefills_existing_note_and_ideal_entry():
    row = _closed_row("AEHL", "trailing_stop")
    row["review_label"] = "lucky"
    row["review_note"] = "gap up saved a marginal entry"
    row["ideal_entry_price"] = 9.85
    rows = _journal_closed_rows_html([row])
    assert "review-form-row" in rows
    assert "hidden" in rows  # the form row starts collapsed
    assert "gap up saved a marginal entry" in rows
    assert "9.85" in rows
    assert "selected" in rows  # the current label is pre-selected in the <select>


def test_closed_row_review_form_survives_a_trade_with_no_review_fields_at_all():
    # Backward compatibility: _closed_row (and any pre-migration-shaped
    # dict) has no review_label/review_note/ideal_entry_price keys at
    # all -- .get(), not direct indexing, must not KeyError.
    rows = _journal_closed_rows_html([_closed_row("AEHL", "trailing_stop")])
    assert "review-form-row" in rows


def test_post_review_stores_label_note_and_ideal_entry_on_a_real_closed_trade(tmp_path):
    from journal_store import JournalStore
    store = JournalStore(tmp_path / "journal.db")
    closed = _seed_closed_trades(store)
    target_id = next(c["id"] for c in closed if c["symbol"] == "AEHL")

    with _client(FakeFetch({}), symbol=None, journal_store=store) as c:
        r = c.post(f"/api/trades/{target_id}/review", json={
            "review_label": "clean_signal",
            "review_note": "held exactly per plan",
            "ideal_entry_price": 10.05,
        })
        assert r.status_code == 200
        assert r.json()["ok"] is True

    reviewed = next(c for c in store.recent_closed() if c["id"] == target_id)
    assert reviewed["review_label"] == "clean_signal"
    assert reviewed["review_note"] == "held exactly per plan"
    assert reviewed["ideal_entry_price"] == 10.05


def test_post_review_on_an_open_trade_is_rejected(tmp_path):
    from journal_store import JournalStore
    store = JournalStore(tmp_path / "journal.db")
    open_pos = store.create(_position(symbol="AEHL"))  # never closed

    with _client(FakeFetch({}), symbol=None, journal_store=store) as c:
        r = c.post(f"/api/trades/{open_pos.id}/review", json={"review_label": "clean_signal"})
        assert r.status_code == 409
        assert r.json()["ok"] is False
        assert "open" in r.json()["reason"].lower()

    # nothing was written
    assert store.open_position_for("AEHL").id == open_pos.id


def test_post_review_on_an_unknown_trade_id_returns_409(tmp_path):
    from journal_store import JournalStore
    store = JournalStore(tmp_path / "journal.db")
    with _client(FakeFetch({}), symbol=None, journal_store=store) as c:
        r = c.post("/api/trades/999999/review", json={"review_label": "clean_signal"})
        assert r.status_code == 409
        assert r.json()["ok"] is False


def test_post_review_twice_updates_in_place_not_a_duplicate(tmp_path):
    from journal_store import JournalStore
    store = JournalStore(tmp_path / "journal.db")
    closed = _seed_closed_trades(store)
    target_id = next(c["id"] for c in closed if c["symbol"] == "AEHL")

    with _client(FakeFetch({}), symbol=None, journal_store=store) as c:
        c.post(f"/api/trades/{target_id}/review", json={"review_label": "bad_signal",
                                                        "review_note": "chased it"})
        r = c.post(f"/api/trades/{target_id}/review", json={"review_label": "clean_signal",
                                                            "review_note": "actually fine"})
        assert r.status_code == 200

    all_closed = store.recent_closed()
    assert len(all_closed) == 3  # still exactly the 3 seeded trades, no duplicate
    reviewed = next(c for c in all_closed if c["id"] == target_id)
    assert reviewed["review_label"] == "clean_signal"
    assert reviewed["review_note"] == "actually fine"


def test_post_review_rejects_an_unrecognized_label(tmp_path):
    from journal_store import JournalStore
    store = JournalStore(tmp_path / "journal.db")
    closed = _seed_closed_trades(store)
    target_id = next(c["id"] for c in closed if c["symbol"] == "AEHL")

    with _client(FakeFetch({}), symbol=None, journal_store=store) as c:
        r = c.post(f"/api/trades/{target_id}/review", json={"review_label": "definitely_not_real"})
        assert r.status_code == 409
        assert r.json()["ok"] is False

    assert next(c for c in store.recent_closed() if c["id"] == target_id)["review_label"] is None


def test_post_review_rejects_an_over_length_note(tmp_path):
    from journal_store import JournalStore
    from journal_store import MAX_REVIEW_NOTE_LENGTH
    store = JournalStore(tmp_path / "journal.db")
    closed = _seed_closed_trades(store)
    target_id = next(c["id"] for c in closed if c["symbol"] == "AEHL")

    with _client(FakeFetch({}), symbol=None, journal_store=store) as c:
        r = c.post(f"/api/trades/{target_id}/review",
                   json={"review_note": "x" * (MAX_REVIEW_NOTE_LENGTH + 1)})
        assert r.status_code == 409
        assert r.json()["ok"] is False


def test_post_review_rejects_a_non_numeric_ideal_entry_price(tmp_path):
    from journal_store import JournalStore
    store = JournalStore(tmp_path / "journal.db")
    closed = _seed_closed_trades(store)
    target_id = next(c["id"] for c in closed if c["symbol"] == "AEHL")

    with _client(FakeFetch({}), symbol=None, journal_store=store) as c:
        r = c.post(f"/api/trades/{target_id}/review",
                   json={"ideal_entry_price": "not-a-number"})
        assert r.status_code == 409
        assert r.json()["ok"] is False


def test_post_review_when_journaling_disabled_returns_409():
    with _client(FakeFetch({}), symbol=None) as c:  # no journal_store
        r = c.post("/api/trades/1/review", json={"review_label": "clean_signal"})
        assert r.status_code == 409
        assert r.json()["ok"] is False


def test_review_displays_correctly_on_the_real_rendered_page(tmp_path):
    from journal_store import JournalStore
    store = JournalStore(tmp_path / "journal.db")
    closed = _seed_closed_trades(store)
    target_id = next(c["id"] for c in closed if c["symbol"] == "AEHL")

    with _client(FakeFetch({}), symbol=None, journal_store=store) as c:
        c.post(f"/api/trades/{target_id}/review", json={
            "review_label": "clean_signal", "review_note": "textbook",
            "ideal_entry_price": 10.05,
        })
        page = c.get("/").text
        assert "review-label-clean_signal" in page
        assert ">clean_signal<" in page
        assert "textbook" in page
