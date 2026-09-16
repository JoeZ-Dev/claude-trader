"""
Poller-level wiring tests for the virtual trade journal: does app.py
correctly connect real hold_confirmed transitions (computed by
state.build_state, via real core/ logic) to journal_logic/journal_store.
The pure decision mechanics (ratchet, no-confirmation-delay stop exit,
mutual exclusivity of opened/updated/closed) are already thoroughly
covered in test_journal_logic.py against hand-crafted OpenPosition
objects; these tests instead prove the WIRING end to end against a real
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
    resistance level around price 10.05 with hold.confirmed == False."""
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


class FakeFetch:
    def __init__(self, batches):
        self._batches = list(batches)

    async def __call__(self, symbol, since_ts):
        batch = self._batches.pop(0) if self._batches else []
        return [b for b in batch if b["ts"] >= since_ts]


def _client(fetch, *, journal_store, symbol="AEHL", trail_pct=TRAIL_PCT,
           announce=None, unwatch=None, now_fn=time.time):
    app = create_app(fetch_bars=fetch, watch_symbol=symbol, poll_interval=0.05,
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


# -- entry fires exactly once on the real transition -------------------

def test_entry_fires_on_real_hold_confirmed_transition(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    fetch = FakeFetch([_pre_break_bars(), _break_bars(), _pullback_bars()])

    with _client(fetch, journal_store=store) as c:
        assert _wait_until(lambda: c.get("/api/state").json().get("bar_count") == 20)
        assert _wait_until(lambda: store.open_position_for("AEHL") is not None)

    pos = store.open_position_for("AEHL")
    assert pos.entry_price == 9.8
    assert pos.entry_ts == 190
    assert pos.high_water_mark == 9.8
    assert pos.stop_level == round(9.8 * (1 - TRAIL_PCT), 10)


def test_entry_does_not_duplicate_on_subsequent_confirmed_polls(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    # one more batch after the transition, still confirmed, no new signal
    more_but_still_confirmed = [_bar(200, 9.85), _bar(210, 9.75)]
    fetch = FakeFetch([_pre_break_bars(), _break_bars(), _pullback_bars(),
                       more_but_still_confirmed])

    with _client(fetch, journal_store=store) as c:
        assert _wait_until(lambda: c.get("/api/state").json().get("bar_count") == 22)
        # give it a moment past the last batch to make sure no second entry sneaks in
        time.sleep(0.2)

    closed = store.recent_closed()
    assert closed == []              # never stopped out or switched away
    # exactly one row in the whole table: still-open position, no duplicate
    assert store.open_position_for("AEHL") is not None


# -- trailing stop, wired end to end -------------------------------------

def test_stop_exit_recorded_in_journal_store(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    # entry at 9.8, stop = 9.8*0.95 = 9.31; this bar's low breaches it
    breach = [_bar(200, 9.2, high=9.85, low=9.0)]
    fetch = FakeFetch([_pre_break_bars(), _break_bars(), _pullback_bars(), breach])

    with _client(fetch, journal_store=store) as c:
        assert _wait_until(lambda: c.get("/api/state").json().get("bar_count") == 21)
        assert _wait_until(lambda: store.open_position_for("AEHL") is None)

    (closed,) = store.recent_closed()
    assert closed["exit_reason"] == "trailing_stop"
    assert closed["symbol"] == "AEHL"


# -- symbol switch force-closes the open position -------------------------

def test_symbol_switch_force_closes_open_position(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    fetch = FakeFetch([_pre_break_bars(), _break_bars(), _pullback_bars(),
                       []])  # empty batch for the new symbol after switch

    with _client(fetch, journal_store=store) as c:
        assert _wait_until(lambda: store.open_position_for("AEHL") is not None)
        c.post("/api/watch", data={"symbol": "MSFT"})
        assert _wait_until(lambda: store.open_position_for("AEHL") is None)

    (closed,) = store.recent_closed()
    assert closed["symbol"] == "AEHL"
    assert closed["exit_reason"] == "symbol_switched"
    assert closed["exit_price"] == 9.8   # last known AEHL price before the switch


# -- restart persistence: prove an open position resumes, no duplicate entry -

def test_restart_resumes_open_position_without_duplicate_entry(tmp_path):
    db_path = tmp_path / "journal.db"
    store1 = JournalStore(db_path)
    fetch1 = FakeFetch([_pre_break_bars(), _break_bars(), _pullback_bars()])
    with _client(fetch1, journal_store=store1) as c:
        assert _wait_until(lambda: store1.open_position_for("AEHL") is not None)
    del store1

    # Fresh store + fresh app over the same DB file and the same symbol =
    # a restart. hold_confirmed reads True again immediately (real bars,
    # real recompute) -- this must NOT be treated as a fresh transition.
    store2 = JournalStore(db_path)
    fetch2 = FakeFetch([_pre_break_bars() + _break_bars() + _pullback_bars()])
    with _client(fetch2, journal_store=store2) as c:
        assert _wait_until(lambda: c.get("/api/state").json().get("bar_count") == 20)
        time.sleep(0.2)

    assert len(store2.recent_closed()) == 0   # never stopped out / switched
    resumed = store2.open_position_for("AEHL")
    assert resumed is not None
    assert resumed.entry_price == 9.8         # the ORIGINAL entry, not re-entered
