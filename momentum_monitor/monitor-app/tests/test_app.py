import asyncio
import os
import sys
import threading
import time

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _APP_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(_APP_DIR), "core"))

from fastapi.testclient import TestClient

from app import create_app

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
            trail_pct=0.05, max_symbols=4, poll_interval=0.05):
    app = create_app(fetch_bars=fetch, watch_symbol=symbol,
                     poll_interval=poll_interval, announce_watch=announce,
                     announce_unwatch=unwatch,
                     announce_retry_attempts=announce_retry_attempts,
                     announce_retry_base_delay=announce_retry_base_delay,
                     announce_retry_max_delay=announce_retry_max_delay,
                     journal_store=journal_store, trail_pct=trail_pct,
                     max_symbols=max_symbols)
    return TestClient(app)


def _sym_state(client, symbol):
    return client.get("/api/state").json()["symbols"].get(symbol)


# -- /api/state shape ------------------------------------------------------

def test_api_state_shape_has_symbols_recent_closed_poll_enabled_max_symbols():
    with _client(FakeFetch({"AEHL": [_bars(3)]})) as c:
        body = c.get("/api/state").json()
        assert set(body) == {"symbols", "recent_closed", "poll_enabled", "max_symbols"}
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
        assert _wait_until(lambda: (_sym_state(c, "AEHL") or {}).get("bar_count", 0) >= 25)
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


def test_post_watch_rejects_a_5th_symbol_with_clear_reason_not_silent_failure():
    fetch = FakeFetch({s: [_bars(2, base=float(i))] for i, s in
                       enumerate(["AEHL", "S2", "S3", "S4", "S5"])})
    with _client(fetch, symbol="AEHL") as c:
        for sym in ("S2", "S3", "S4"):
            assert c.post("/api/watch", data={"symbol": sym}).json()["ok"] is True
        assert set(c.get("/api/state").json()["symbols"]) == {"AEHL", "S2", "S3", "S4"}

        r = c.post("/api/watch", data={"symbol": "S5"})
        assert r.status_code == 409
        body = r.json()
        assert body["ok"] is False
        assert "4" in body["reason"] or "maximum" in body["reason"].lower()
        # the 4 existing slots must be completely unchanged -- no silent replacement
        assert set(body["symbols"]) == {"AEHL", "S2", "S3", "S4"}
        assert "S5" not in body["symbols"]


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


def test_removing_a_symbol_frees_a_slot_for_a_new_add():
    fetch = FakeFetch({s: [_bars(2, base=float(i))] for i, s in
                       enumerate(["AEHL", "S2", "S3", "S4", "S5"])})
    with _client(fetch, symbol="AEHL") as c:
        for sym in ("S2", "S3", "S4"):
            c.post("/api/watch", data={"symbol": sym})
        assert c.post("/api/watch", data={"symbol": "S5"}).json()["ok"] is False

        c.post("/api/unwatch", data={"symbol": "S2"})
        r = c.post("/api/watch", data={"symbol": "S5"})
        assert r.json()["ok"] is True
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


def test_poll_control_pauses_the_background_poller_for_all_symbols():
    fetch = FakeFetch({"AEHL": [_bars(3)], "S2": [_bars(3, base=20.0)]})
    with _client(fetch, symbol="AEHL") as c:
        c.post("/api/watch", data={"symbol": "S2"})
        assert _wait_until(lambda: all(
            (_sym_state(c, s) or {}).get("status") == "ok" for s in ("AEHL", "S2")
        ))
        r = c.post("/api/polling", json={"enabled": False})
        assert r.json()["poll_enabled"] is False
        assert _wait_until(lambda: c.get("/api/state").json()["poll_enabled"] is False)

        calls_at_pause = len(fetch.calls)
        time.sleep(0.3)
        assert len(fetch.calls) == calls_at_pause, \
            "fetch_bars was called again after pausing -- polling didn't stop for all symbols"


def test_poll_control_resumes_the_background_poller():
    fetch = FakeFetch({"AEHL": [_bars(3)]})
    with _client(fetch) as c:
        assert _wait_until(lambda: (_sym_state(c, "AEHL") or {}).get("status") == "ok")
        c.post("/api/polling", json={"enabled": False})
        assert _wait_until(lambda: c.get("/api/state").json()["poll_enabled"] is False)
        calls_while_paused = len(fetch.calls)

        r = c.post("/api/polling", json={"enabled": True})
        assert r.json()["poll_enabled"] is True
        assert _wait_until(lambda: len(fetch.calls) > calls_while_paused)


# -- page rendering (Stage A interim) ---------------------------------------

def test_root_page_has_no_meta_refresh():
    with _client(FakeFetch({"AEHL": [_bars(5)]})) as c:
        assert 'http-equiv="refresh"' not in c.get("/").text.lower()


def test_root_page_polls_api_state_via_js_without_reloading():
    with _client(FakeFetch({"AEHL": [_bars(5)]})) as c:
        page = c.get("/").text
        assert "setInterval" in page
        assert "fetch(" in page
        assert "/api/state" in page


def test_root_page_has_a_pause_polling_toggle():
    with _client(FakeFetch({"AEHL": [_bars(5)]})) as c:
        page = c.get("/").text
        assert "clearInterval" in page
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
