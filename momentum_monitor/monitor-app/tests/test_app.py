import os
import sys
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


def _client(fetch, *, symbol="AEHL", announce=None):
    app = create_app(fetch_bars=fetch, watch_symbol=symbol,
                     poll_interval=0.05, announce_watch=announce)
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


def test_no_symbol_configured_stays_warming_up():
    fetch = FakeFetch([_bars(10)])
    with _client(fetch, symbol=None) as c:
        time.sleep(0.2)
        assert c.get("/api/state").json()["status"] == "warming_up"
        assert fetch.calls == []


def test_api_state_survives_fetch_error():
    fetch = FakeFetch([_bars(20), _bars(5, start=RTH + 200, base=12.0)])
    with _client(fetch) as c:
        assert _wait_until(lambda: c.get("/api/state").json().get("status") == "ok")
        fetch.raise_next = True
        time.sleep(0.2)
        r = c.get("/api/state")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"  # last good state retained
