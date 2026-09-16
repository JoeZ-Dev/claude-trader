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


def _app(tmp_path, *, history_fetcher=None):
    fx = _fixture(tmp_path)
    store = BarStore(tmp_path / "bars")
    return create_app(
        store=store,
        source_factory=lambda: ReplayStreamSource(fx),
        replay=True,
        now_fn=lambda: RTH_1030 + 30,
        history_fetcher=history_fetcher,
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
        source_factory=lambda: ReplayStreamSource(fx),
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
