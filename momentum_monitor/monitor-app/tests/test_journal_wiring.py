"""
Poller-level wiring tests for the virtual trade journal: does app.py
correctly connect real hold_confirmed transitions (computed by
state.build_state, via real core/ logic) to journal_logic/journal_store,
now per-symbol (phase 2, up to 4 concurrent). The pure decision mechanics
(ratchet, no-confirmation-delay stop exit, mutual exclusivity of
opened/updated/closed) are already thoroughly covered in
test_journal_logic.py against hand-crafted OpenPosition objects; these
tests instead prove the WIRING end to end against a real
JournalStore(tmp_path) and bar sequences verified (by direct experiment
against core/, not assumed) to produce an actual False->True
hold_confirmed transition through the real detect_levels/select_levels/
evaluate_hold pipeline -- not a stubbed-out state dict.
"""
import os
import sys
import time

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _APP_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(_APP_DIR), "core"))

from fastapi.testclient import TestClient

from app import create_app
from journal_store import JournalStore

TRAIL_PCT = 0.05


def _bar(ts, price, *, high=None, low=None, vol=50_000.0):
    return {"ts": ts, "open": price, "high": high if high is not None else price + 0.05,
            "low": low if low is not None else price - 0.05, "close": price,
            "volume": vol, "is_extended": False}


def _pre_break_bars():
    """Verified via direct experiment against core/'s real detect_levels/
    select_levels/evaluate_hold: this oscillation produces a selected
    resistance level around price 10.05 with hold.confirmed == False. Same
    fixture, reused for any symbol -- the price pattern doesn't care what
    ticker it's attached to."""
    bars = []
    ts = 0
    for _ in range(3):
        for p in (9.0, 9.5, 10.0, 9.6, 9.1):
            bars.append(_bar(ts, p))
            ts += 10
    return bars  # ts 0..140


def _break_bars():
    """3 consecutive closes above the 10.05 resistance -- while price
    stays above it, select_levels won't show it as resistance at all
    (it only selects levels priced ABOVE the current price), so no
    transition is observable yet during this batch."""
    return [_bar(150, 10.3), _bar(160, 10.4), _bar(170, 10.5)]


def _pullback_bars():
    """Verified: once price pulls back below 10.05 again, select_levels
    shows it as resistance once more, and evaluate_hold (which walks the
    WHOLE bar history) now reports hold.confirmed == True, because the
    break above held for 3 consecutive closes earlier in this same
    history. This is the real False->True transition."""
    return [_bar(180, 9.9), _bar(190, 9.8)]  # entry should fire here, at 9.8


def _full_entry_sequence():
    return _pre_break_bars() + _break_bars() + _pullback_bars()


class FakeFetch:
    """Batches queued PER SYMBOL (phase 2: multiple symbols polled
    independently) -- each call for a given symbol pops that symbol's own
    next queued batch."""

    def __init__(self, batches_by_symbol):
        self._queues = {k.upper(): list(v) for k, v in batches_by_symbol.items()}

    async def __call__(self, symbol, since_ts):
        q = self._queues.get(symbol)
        batch = q.pop(0) if q else []
        return [b for b in batch if b["ts"] >= since_ts]


def _client(fetch, *, journal_store, symbol="AEHL", trail_pct=TRAIL_PCT,
           announce=None, unwatch=None, now_fn=time.time):
    app = create_app(fetch_bars=fetch, watch_symbol=symbol,
                     announce_watch=announce, announce_unwatch=unwatch,
                     announce_retry_attempts=1,
                     journal_store=journal_store, trail_pct=trail_pct,
                     now_fn=now_fn)
    return TestClient(app)


def _wait_until(pred, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.03)
    return pred()


def _resync(c):
    """Advances every watched symbol by one queued FakeFetch batch --
    there's no more background poll timer doing this on its own (fixed
    2026-09-17, see specs.md), so these tests drive it explicitly the
    same way a real resync_all() would fire, via a pause/resume round
    trip on the already-existing POST /api/polling toggle (both calls
    are awaited fully server-side before responding, so this is
    synchronous from the caller's point of view -- no sleep needed
    between calling this and checking its effect)."""
    c.post("/api/polling", json={"enabled": False})
    c.post("/api/polling", json={"enabled": True})


def _sym(client, symbol):
    return client.get("/api/state").json()["symbols"].get(symbol) or {}


# -- entry fires exactly once on the real transition -------------------

def test_entry_fires_on_real_hold_confirmed_transition(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    fetch = FakeFetch({"AEHL": [_pre_break_bars(), _break_bars(), _pullback_bars()]})

    with _client(fetch, journal_store=store) as c:
        assert _wait_until(lambda: _sym(c, "AEHL").get("bar_count") == 15)  # initial catch_up
        _resync(c)  # break_bars
        _resync(c)  # pullback_bars -- the real transition
        assert _sym(c, "AEHL")["bar_count"] == 20
        assert store.open_position_for("AEHL") is not None

    pos = store.open_position_for("AEHL")
    assert pos.entry_price == 9.8
    assert pos.entry_ts == 190
    assert pos.high_water_mark == 9.8
    assert pos.stop_level == round(9.8 * (1 - TRAIL_PCT), 10)


def test_entry_does_not_duplicate_on_subsequent_confirmed_polls(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    more_but_still_confirmed = [_bar(200, 9.85), _bar(210, 9.75)]
    fetch = FakeFetch({"AEHL": [_pre_break_bars(), _break_bars(), _pullback_bars(),
                                more_but_still_confirmed]})

    with _client(fetch, journal_store=store) as c:
        assert _wait_until(lambda: _sym(c, "AEHL").get("bar_count") == 15)
        _resync(c)  # break_bars
        _resync(c)  # pullback_bars -- the real transition, entry fires
        _resync(c)  # more_but_still_confirmed -- make sure no second entry sneaks in
        assert _sym(c, "AEHL")["bar_count"] == 22

    assert store.recent_closed() == []
    assert store.open_position_for("AEHL") is not None


def test_root_page_renders_open_position_after_real_entry(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    fetch = FakeFetch({"AEHL": [_pre_break_bars(), _break_bars(), _pullback_bars()]})

    with _client(fetch, journal_store=store) as c:
        assert _wait_until(lambda: _sym(c, "AEHL").get("bar_count") == 15)
        _resync(c)  # break_bars
        _resync(c)  # pullback_bars -- the real transition
        assert store.open_position_for("AEHL") is not None
        page = c.get("/").text
        # the real, meaningful proof is the entry price actually showing up
        # in the server-rendered card content, not a generic string match
        assert "9.8" in page


# -- trailing stop, wired end to end -------------------------------------

def test_stop_exit_recorded_in_journal_store(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    breach = [_bar(200, 9.2, high=9.85, low=9.0)]  # entry@9.8, stop=9.31, breached
    fetch = FakeFetch({"AEHL": [_pre_break_bars(), _break_bars(), _pullback_bars(), breach]})

    with _client(fetch, journal_store=store) as c:
        assert _wait_until(lambda: _sym(c, "AEHL").get("bar_count") == 15)
        _resync(c)  # break_bars
        _resync(c)  # pullback_bars -- entry fires
        _resync(c)  # breach -- trailing stop trips
        assert _sym(c, "AEHL")["bar_count"] == 21
        assert store.open_position_for("AEHL") is None

    (closed,) = store.recent_closed()
    assert closed["exit_reason"] == "trailing_stop"
    assert closed["symbol"] == "AEHL"


# -- removing a symbol force-closes its open position ------------------

def test_unwatch_force_closes_open_position(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    fetch = FakeFetch({"AEHL": [_pre_break_bars(), _break_bars(), _pullback_bars()]})

    with _client(fetch, journal_store=store) as c:
        assert _wait_until(lambda: _sym(c, "AEHL").get("bar_count") == 15)
        _resync(c)  # break_bars
        _resync(c)  # pullback_bars -- the real transition
        assert store.open_position_for("AEHL") is not None
        c.post("/api/unwatch", data={"symbol": "AEHL"})
        assert _wait_until(lambda: store.open_position_for("AEHL") is None)

    (closed,) = store.recent_closed()
    assert closed["symbol"] == "AEHL"
    assert closed["exit_reason"] == "symbol_switched"
    assert closed["exit_price"] == 9.8   # last known AEHL price before removal


# -- restart persistence: prove an open position resumes, no duplicate entry -

def test_restart_resumes_open_position_without_duplicate_entry(tmp_path):
    db_path = tmp_path / "journal.db"
    store1 = JournalStore(db_path)
    fetch1 = FakeFetch({"AEHL": [_pre_break_bars(), _break_bars(), _pullback_bars()]})
    with _client(fetch1, journal_store=store1) as c:
        assert _wait_until(lambda: _sym(c, "AEHL").get("bar_count") == 15)
        _resync(c)  # break_bars
        _resync(c)  # pullback_bars -- the real transition
        assert store1.open_position_for("AEHL") is not None
    del store1

    store2 = JournalStore(db_path)
    fetch2 = FakeFetch({"AEHL": [_full_entry_sequence()]})
    with _client(fetch2, journal_store=store2) as c:
        assert _wait_until(lambda: _sym(c, "AEHL").get("bar_count") == 20)
        time.sleep(0.2)

    assert len(store2.recent_closed()) == 0
    resumed = store2.open_position_for("AEHL")
    assert resumed is not None
    assert resumed.entry_price == 9.8


# -- phase 2: two symbols' journal positions are fully independent --------

def test_two_symbols_journal_positions_are_fully_independent(tmp_path):
    # AEHL: full lifecycle -- enters, then stops out.
    # MSFT: enters via the SAME real transition, but never breaches its
    # stop -- must stay open, completely undisturbed by AEHL's stop-out.
    # This is the direct test of the requirement: "a trailing-stop exit on
    # one symbol must not affect another symbol's open position or
    # trigger anything on it."
    store = JournalStore(tmp_path / "journal.db")
    breach = [_bar(200, 9.2, high=9.85, low=9.0)]  # AEHL only: breaches its 9.31 stop
    fetch = FakeFetch({
        # AEHL gets a 4th batch (breach); MSFT's queue ends at 3 -- resync_all
        # advances every watched symbol in lockstep, one queued batch each,
        # so giving MSFT nothing further to consume is what actually proves
        # it's undisturbed (a no-op catch_up, not raced against AEHL's).
        "AEHL": [_pre_break_bars(), _break_bars(), _pullback_bars(), breach],
        "MSFT": [_pre_break_bars(), _break_bars(), _pullback_bars()],
    })

    with _client(fetch, journal_store=store, symbol="AEHL") as c:
        c.post("/api/watch", data={"symbol": "MSFT"})
        assert _wait_until(lambda: all(
            (_sym(c, s) or {}).get("bar_count") == 15 for s in ("AEHL", "MSFT")
        ))
        _resync(c)  # break_bars, both symbols
        _resync(c)  # pullback_bars, both symbols -- both enter
        assert _wait_until(lambda: all(
            store.open_position_for(s) is not None for s in ("AEHL", "MSFT")
        ))
        msft_before = store.open_position_for("MSFT")

        # drive AEHL to its stop-out (its 4th batch; MSFT's queue is
        # already exhausted, so this resync is a harmless no-op for it)
        _resync(c)
        assert store.open_position_for("AEHL") is None

        # MSFT must be completely unaffected: still open, identical values
        msft_after = store.open_position_for("MSFT")
        assert msft_after is not None
        assert msft_after.id == msft_before.id
        assert msft_after.entry_price == msft_before.entry_price == 9.8
        assert msft_after.high_water_mark == msft_before.high_water_mark
        assert msft_after.stop_level == msft_before.stop_level

    closed = store.recent_closed()
    assert len(closed) == 1  # only AEHL's stop-out, nothing for MSFT
    assert closed[0]["symbol"] == "AEHL"
    assert closed[0]["exit_reason"] == "trailing_stop"

    # MSFT's row is still open in the DB, not just in the in-memory slot
    assert store.open_position_for("MSFT") is not None
    assert store.open_position_for("AEHL") is None


def test_removing_one_symbol_does_not_close_another_symbols_position(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    fetch = FakeFetch({
        "AEHL": [_pre_break_bars(), _break_bars(), _pullback_bars()],
        "MSFT": [_pre_break_bars(), _break_bars(), _pullback_bars()],
    })

    with _client(fetch, journal_store=store, symbol="AEHL") as c:
        c.post("/api/watch", data={"symbol": "MSFT"})
        assert _wait_until(lambda: all(
            (_sym(c, s) or {}).get("bar_count") == 15 for s in ("AEHL", "MSFT")
        ))
        _resync(c)  # break_bars, both symbols
        _resync(c)  # pullback_bars, both symbols -- both enter
        assert store.open_position_for("AEHL") is not None \
            and store.open_position_for("MSFT") is not None

        c.post("/api/unwatch", data={"symbol": "AEHL"})
        assert _wait_until(lambda: store.open_position_for("AEHL") is None)

        # MSFT's position must survive AEHL's removal untouched
        assert store.open_position_for("MSFT") is not None

    closed = store.recent_closed()
    assert len(closed) == 1
    assert closed[0]["symbol"] == "AEHL"
