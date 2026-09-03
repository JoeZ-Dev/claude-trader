import json
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


def _app(tmp_path):
    fx = _fixture(tmp_path)
    store = BarStore(tmp_path / "bars")
    return create_app(
        store=store,
        source_factory=lambda: ReplayStreamSource(fx),
        replay=True,
        now_fn=lambda: RTH_1030 + 30,
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
    def broken_factory():
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


def test_bars_survive_new_app_on_same_store_dir(tmp_path):
    app1, _ = _app(tmp_path)
    with TestClient(app1) as c:
        c.post("/watch", json={"symbol": "AEHL"})
        _wait_for_bars(c, "AEHL", want=3)

    # Fresh app + fresh BarStore over the same directory = a restart.
    fx = _fixture(tmp_path)
    app2 = create_app(
        store=BarStore(tmp_path / "bars"),
        source_factory=lambda: ReplayStreamSource(fx),
        replay=True,
        now_fn=lambda: RTH_1030 + 30,
    )
    with TestClient(app2) as c:
        assert [b["ts"] for b in c.get("/bars/AEHL").json()] == \
            [b["ts"] for b in FIXTURE_BARS]
