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
    """Async stand-in for the httpx call to schwab-connector /bars."""

    def __init__(self, batches):
        self._batches = list(batches)
        self.calls = []          # since_ts values it was asked for
        self.raise_next = False

    async def __call__(self, symbol, since_ts):
        self.calls.append(since_ts)
        if self.raise_next:
            self.raise_next = False
            raise RuntimeError("connector unreachable")
        if self._batches:
            batch = self._batches.pop(0)
        else:
            batch = []
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
            announce_retry_max_delay=0.02):
    app = create_app(fetch_bars=fetch, watch_symbol=symbol,
                     poll_interval=0.05, announce_watch=announce,
                     announce_unwatch=unwatch,
                     announce_retry_attempts=announce_retry_attempts,
                     announce_retry_base_delay=announce_retry_base_delay,
                     announce_retry_max_delay=announce_retry_max_delay)
    return TestClient(app)


def test_api_state_warming_up_before_any_bars():
    with _client(FakeFetch([[]])) as c:
        assert c.get("/api/state").json()["status"] == "warming_up"


def test_api_state_reflects_fetched_bars():
    bars = _bars(30)
    with _client(FakeFetch([bars])) as c:
        assert _wait_until(lambda: c.get("/api/state").json().get("status") == "ok")
        st = c.get("/api/state").json()
        assert st["bar_count"] == 30
        assert st["symbol"] == "AEHL"
        assert st["last_price"] == round(bars[-1]["close"], 4)


def test_poller_advances_since_ts_and_dedups_boundary_bar():
    first = _bars(20)
    # second batch overlaps on the last ts of the first (inclusive endpoint)
    overlap = first[-1]
    second = [overlap] + _bars(5, start=overlap["ts"] + 10, base=11.0)
    fetch = FakeFetch([first, second])
    with _client(fetch) as c:
        assert _wait_until(lambda: c.get("/api/state").json().get("bar_count", 0) >= 25)
        st = c.get("/api/state").json()
        assert st["bar_count"] == 25          # 20 + 5, overlap bar not double-counted
        assert any(s > 0 for s in fetch.calls)  # cursor advanced past 0


def test_root_page_renders_key_numbers():
    bars = _bars(30)
    with _client(FakeFetch([bars])) as c:
        assert _wait_until(lambda: c.get("/api/state").json().get("status") == "ok")
        html = c.get("/").text
        assert "AEHL" in html
        assert "VWAP" in html.upper()
        assert 'http-equiv="refresh"' in html


def test_announce_watch_called_on_startup():
    seen = []

    async def announce(sym):
        seen.append(sym)

    with _client(FakeFetch([_bars(5)]), announce=announce) as c:
        assert _wait_until(lambda: seen == ["AEHL"])


def test_announce_watch_retries_and_recovers_after_transient_failures():
    calls = []

    async def flaky_announce(sym):
        calls.append(sym)
        if len(calls) < 3:            # first two attempts fail
            raise RuntimeError("connector not listening yet")
        # third attempt succeeds

    with _client(FakeFetch([_bars(5)]), announce=flaky_announce) as c:
        assert _wait_until(lambda: len(calls) == 3)
        # confirm it actually reached the "watched and working" end state,
        # not just that the retry loop ran three times
        assert _wait_until(lambda: c.get("/api/state").json().get("status") == "ok")


def test_announce_watch_gives_up_after_exhausting_retries_but_keeps_polling():
    async def always_fails(sym):
        raise RuntimeError("connector unreachable")

    fetch = FakeFetch([_bars(5)])
    with _client(fetch, announce=always_fails, announce_retry_attempts=2) as c:
        # polling still proceeds even though announce_watch never succeeds
        assert _wait_until(lambda: c.get("/api/state").json().get("status") == "ok")


def test_no_symbol_configured_stays_warming_up():
    fetch = FakeFetch([_bars(10)])
    with _client(fetch, symbol=None) as c:
        time.sleep(0.2)
        assert c.get("/api/state").json()["status"] == "warming_up"
        assert fetch.calls == []


# -- switching the watched symbol at runtime, via POST /api/watch --------

def test_watch_form_present_on_root_page():
    with _client(FakeFetch([[]]), symbol=None) as c:
        page = c.get("/").text
        assert "/api/watch" in page
        assert "<input" in page


def test_no_symbol_shows_prompt_to_enter_one():
    with _client(FakeFetch([[]]), symbol=None) as c:
        assert "enter a ticker" in c.get("/").text.lower()


def test_post_watch_starts_watching_a_new_symbol():
    seen = []

    async def announce(sym):
        seen.append(sym)

    fetch = FakeFetch([_bars(5, base=50.0)])
    with _client(fetch, symbol=None, announce=announce) as c:
        r = c.post("/api/watch", data={"symbol": "msft"})
        assert r.status_code in (200, 303)
        assert _wait_until(lambda: c.get("/api/state").json().get("symbol") == "MSFT")
        assert _wait_until(lambda: c.get("/api/state").json().get("status") == "ok")
        assert seen == ["MSFT"]


def test_post_watch_redirects_to_root():
    with _client(FakeFetch([[]]), symbol=None) as c:
        r = c.post("/api/watch", data={"symbol": "MSFT"}, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/"


def test_post_watch_resets_bars_when_switching_symbols():
    fetch = FakeFetch([_bars(30), _bars(3, base=50.0)])
    with _client(fetch) as c:  # starts on AEHL
        assert _wait_until(lambda: c.get("/api/state").json().get("bar_count") == 30)
        c.post("/api/watch", data={"symbol": "MSFT"})
        assert _wait_until(lambda: c.get("/api/state").json().get("symbol") == "MSFT")
        assert _wait_until(lambda: c.get("/api/state").json().get("bar_count") == 3)


def test_post_watch_same_symbol_is_a_noop():
    fetch = FakeFetch([_bars(30)])
    with _client(fetch) as c:
        assert _wait_until(lambda: c.get("/api/state").json().get("bar_count") == 30)
        c.post("/api/watch", data={"symbol": "aehl"})
        time.sleep(0.15)
        # still 30 -- no reset, and only one batch was ever queued for fetch
        assert c.get("/api/state").json()["bar_count"] == 30


def test_post_watch_ignores_blank_symbol():
    fetch = FakeFetch([_bars(10)])
    with _client(fetch) as c:
        assert _wait_until(lambda: c.get("/api/state").json().get("status") == "ok")
        c.post("/api/watch", data={"symbol": "   "})
        time.sleep(0.1)
        assert c.get("/api/state").json()["symbol"] == "AEHL"


def test_post_watch_rejects_invalid_characters():
    fetch = FakeFetch([_bars(10)])
    with _client(fetch) as c:
        assert _wait_until(lambda: c.get("/api/state").json().get("status") == "ok")
        c.post("/api/watch", data={"symbol": "AB/CD"})
        time.sleep(0.1)
        assert c.get("/api/state").json()["symbol"] == "AEHL"


def test_starting_with_no_symbol_then_watching_one_still_works():
    fetch = FakeFetch([_bars(4, base=20.0)])
    with _client(fetch, symbol=None) as c:
        assert c.get("/api/state").json()["status"] == "warming_up"
        assert fetch.calls == []
        c.post("/api/watch", data={"symbol": "TSLA"})
        assert _wait_until(lambda: c.get("/api/state").json().get("status") == "ok")


# -- switching must unwatch the previous symbol, not just add the new one -
#
# Real gap found live (2026-09-16): schwab-connector accumulated 6
# simultaneously-watched symbols (DLXY, KXIN, QCLS, RETO, SPCX, SPY) from
# using the UI's ticker box repeatedly, despite the UI only ever showing
# one. switch_symbol announced the new watch but never told
# schwab-connector to drop the old one.

def test_switch_symbol_unwatches_the_previous_symbol():
    unwatched = []

    async def unwatch(sym):
        unwatched.append(sym)

    with _client(FakeFetch([_bars(3), _bars(3, base=50.0)]), symbol="AEHL",
                unwatch=unwatch) as c:
        assert _wait_until(lambda: c.get("/api/state").json().get("status") == "ok")
        c.post("/api/watch", data={"symbol": "MSFT"})
        assert _wait_until(lambda: unwatched == ["AEHL"])


def test_first_watch_with_no_prior_symbol_does_not_call_unwatch():
    unwatched = []

    async def unwatch(sym):
        unwatched.append(sym)

    fetch = FakeFetch([_bars(3)])
    with _client(fetch, symbol=None, unwatch=unwatch) as c:
        c.post("/api/watch", data={"symbol": "MSFT"})
        assert _wait_until(lambda: c.get("/api/state").json().get("symbol") == "MSFT")
        time.sleep(0.1)
        assert unwatched == []


def test_switching_to_same_symbol_does_not_call_unwatch():
    unwatched = []

    async def unwatch(sym):
        unwatched.append(sym)

    with _client(FakeFetch([_bars(3)]), symbol="AEHL", unwatch=unwatch) as c:
        assert _wait_until(lambda: c.get("/api/state").json().get("status") == "ok")
        c.post("/api/watch", data={"symbol": "aehl"})
        time.sleep(0.1)
        assert unwatched == []


def test_unwatch_failure_does_not_block_switching_to_new_symbol():
    async def failing_unwatch(sym):
        raise RuntimeError("schwab-connector unreachable")

    with _client(FakeFetch([_bars(3), _bars(3, base=50.0)]), symbol="AEHL",
                unwatch=failing_unwatch) as c:
        assert _wait_until(lambda: c.get("/api/state").json().get("status") == "ok")
        c.post("/api/watch", data={"symbol": "MSFT"})
        assert _wait_until(lambda: c.get("/api/state").json().get("symbol") == "MSFT")
        assert _wait_until(lambda: c.get("/api/state").json().get("status") == "ok")


def test_switch_symbol_during_inflight_poll_discards_stale_result():
    # A switch_symbol() landing while a poll for the OLD symbol is still
    # in flight must not let that in-flight fetch's result get appended to
    # the NEW symbol's (just-reset) bar list.
    release = threading.Event()
    calls = []

    async def fetch(symbol, since_ts):
        calls.append((symbol, since_ts))
        if symbol == "AEHL" and len(calls) == 1:
            while not release.is_set():
                await asyncio.sleep(0.01)
            return _bars(5)  # stale by the time it returns -- must be discarded
        if symbol == "MSFT":
            return _bars(3, base=50.0)
        return []

    with _client(fetch, symbol="AEHL") as c:
        time.sleep(0.1)  # let the first (now-blocked) AEHL fetch start
        c.post("/api/watch", data={"symbol": "MSFT"})
        release.set()
        assert _wait_until(lambda: c.get("/api/state").json().get("symbol") == "MSFT")
        assert _wait_until(lambda: c.get("/api/state").json().get("status") == "ok")
        assert c.get("/api/state").json()["bar_count"] == 3


def test_api_state_survives_fetch_error():
    fetch = FakeFetch([_bars(20), _bars(5, start=RTH + 200, base=12.0)])
    with _client(fetch) as c:
        assert _wait_until(lambda: c.get("/api/state").json().get("status") == "ok")
        fetch.raise_next = True
        time.sleep(0.2)
        r = c.get("/api/state")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"  # last good state retained
