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


def _entry_bars():
    """Verified via direct experiment against core/'s real setup_types
    pipeline (2026-09-17, generalized entry -- see specs.md): this
    oscillation (9.0/9.5/10.0/9.6/9.1, three cycles) produces a clean
    round_number_reclaim confirmation (trigger 9.25, the round-number
    grid point above the final close 9.1) with NOTHING else confirmed at
    the same tick -- entry fires on this batch's last bar, entry_price
    9.1, entry_ts 140. Same fixture, reused for any symbol -- the price
    pattern doesn't care what ticker it's attached to. (This used to also
    carry a resistance-breakout scenario via two more batches, back when
    entry only fired on resistance specifically; generalizing entry to
    all four types made round_number_reclaim -- "always present," per
    setup_types.py -- confirm first here, so those extra batches were
    replaced with the ones below, verified clean of the SAME type
    re-cascading immediately after a stop-out.)"""
    bars = []
    ts = 0
    for _ in range(3):
        for p in (9.0, 9.5, 10.0, 9.6, 9.1):
            bars.append(_bar(ts, p))
            ts += 10
    return bars  # ts 0..140


def _ratchet_bars():
    """Two more bars, still near 9.1, that raise the high-water-mark a
    little without breaching the stop or producing any new confirmation
    -- verified the position simply ratchets (JournalTick.updated), no
    duplicate entry."""
    return [_bar(150, 9.15), _bar(160, 9.05)]


def _sharp_breach_bar():
    """One bar dropping straight to 8.0 -- well below the ratcheted stop.
    Verified this does NOT retrigger a fresh round_number_reclaim
    confirmation the way a gradual multi-bar walk-down through several
    round-number grid lines can (each intermediate band picking up its
    own "3 consecutive closes above it" from the earlier oscillation) --
    a single sharp drop doesn't create that 3-consecutive-bar pattern
    against any new, lower trigger, so this closes cleanly with nothing
    reopening in the same tick."""
    return [_bar(200, 8.0, high=8.6, low=7.8)]


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
           announce=None, unwatch=None, now_fn=time.time,
           volume_confirm_threshold=0.0):
    # Threshold defaults to 0.0 (always clears) -- this file proves the
    # Poller<->journal_logic<->journal_store WIRING against real bar-
    # driven hold_confirmed transitions, not the volume gate itself
    # (test_journal_logic.py's advance_journal tests own that; these
    # fixtures' flat per-bar volume produces relative_volume ~= 1.0,
    # which is a real "no genuine spike" reading, not a fixture bug --
    # gating it off here keeps that concern out of the wiring tests it
    # would otherwise silently couple to).
    app = create_app(fetch_bars=fetch, watch_symbol=symbol,
                     announce_watch=announce, announce_unwatch=unwatch,
                     announce_retry_attempts=1,
                     journal_store=journal_store, trail_pct=trail_pct,
                     volume_confirm_threshold=volume_confirm_threshold,
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
    fetch = FakeFetch({"AEHL": [_entry_bars()]})

    with _client(fetch, journal_store=store) as c:
        assert _wait_until(lambda: _sym(c, "AEHL").get("bar_count") == 15)
        assert store.open_position_for("AEHL") is not None

    pos = store.open_position_for("AEHL")
    assert pos.entry_price == 9.1
    assert pos.entry_ts == 140
    assert pos.high_water_mark == 9.1
    assert pos.stop_level == round(9.1 * (1 - TRAIL_PCT), 10)
    assert pos.setup_type == "round_number_reclaim"


def test_entry_does_not_duplicate_on_subsequent_confirmed_polls(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    fetch = FakeFetch({"AEHL": [_entry_bars(), _ratchet_bars()]})

    with _client(fetch, journal_store=store) as c:
        assert _wait_until(lambda: _sym(c, "AEHL").get("bar_count") == 15)
        entered = store.open_position_for("AEHL")
        _resync(c)  # _ratchet_bars -- make sure no second entry sneaks in
        assert _sym(c, "AEHL")["bar_count"] == 17

    assert store.recent_closed() == []
    still_open = store.open_position_for("AEHL")
    assert still_open is not None
    assert still_open.id == entered.id       # SAME position, not a duplicate
    assert still_open.high_water_mark >= entered.high_water_mark  # ratcheted, not reopened


def test_root_page_renders_open_position_after_real_entry(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    fetch = FakeFetch({"AEHL": [_entry_bars()]})

    with _client(fetch, journal_store=store) as c:
        assert _wait_until(lambda: _sym(c, "AEHL").get("bar_count") == 15)
        assert store.open_position_for("AEHL") is not None
        page = c.get("/").text
        # the real, meaningful proof is the entry price actually showing up
        # in the server-rendered card content, not a generic string match
        assert "9.1" in page


# -- trailing stop, wired end to end -------------------------------------

def test_stop_exit_recorded_in_journal_store(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    fetch = FakeFetch({"AEHL": [_entry_bars(), _ratchet_bars(), _sharp_breach_bar()]})

    with _client(fetch, journal_store=store) as c:
        assert _wait_until(lambda: _sym(c, "AEHL").get("bar_count") == 15)
        entered = store.open_position_for("AEHL")
        _resync(c)  # ratchet
        _resync(c)  # sharp breach -- trailing stop trips
        assert _sym(c, "AEHL")["bar_count"] == 18
        assert store.open_position_for("AEHL") is None

    (closed,) = store.recent_closed()
    assert closed["id"] == entered.id
    assert closed["exit_reason"] == "trailing_stop"
    assert closed["symbol"] == "AEHL"
    assert closed["setup_type"] == "round_number_reclaim"


# -- removing a symbol force-closes its open position ------------------

def test_unwatch_force_closes_open_position(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    fetch = FakeFetch({"AEHL": [_entry_bars()]})

    with _client(fetch, journal_store=store) as c:
        assert _wait_until(lambda: _sym(c, "AEHL").get("bar_count") == 15)
        assert store.open_position_for("AEHL") is not None
        c.post("/api/unwatch", data={"symbol": "AEHL"})
        assert _wait_until(lambda: store.open_position_for("AEHL") is None)

    (closed,) = store.recent_closed()
    assert closed["symbol"] == "AEHL"
    assert closed["exit_reason"] == "symbol_switched"
    assert closed["exit_price"] == 9.1   # last known AEHL price before removal


# -- restart persistence: prove an open position resumes, no duplicate entry -

def test_restart_resumes_open_position_without_duplicate_entry(tmp_path):
    db_path = tmp_path / "journal.db"
    store1 = JournalStore(db_path)
    fetch1 = FakeFetch({"AEHL": [_entry_bars()]})
    with _client(fetch1, journal_store=store1) as c:
        assert _wait_until(lambda: _sym(c, "AEHL").get("bar_count") == 15)
        assert store1.open_position_for("AEHL") is not None
    del store1

    store2 = JournalStore(db_path)
    fetch2 = FakeFetch({"AEHL": [_entry_bars()]})
    with _client(fetch2, journal_store=store2) as c:
        assert _wait_until(lambda: _sym(c, "AEHL").get("bar_count") == 15)
        time.sleep(0.2)

    assert len(store2.recent_closed()) == 0
    resumed = store2.open_position_for("AEHL")
    assert resumed is not None
    assert resumed.entry_price == 9.1
    assert resumed.setup_type == "round_number_reclaim"


# -- phase 2: two symbols' journal positions are fully independent --------

def test_two_symbols_journal_positions_are_fully_independent(tmp_path):
    # AEHL: full lifecycle -- enters, then stops out.
    # MSFT: enters via the SAME real transition, but never breaches its
    # stop -- must stay open, completely undisturbed by AEHL's stop-out.
    # This is the direct test of the requirement: "a trailing-stop exit on
    # one symbol must not affect another symbol's open position or
    # trigger anything on it."
    store = JournalStore(tmp_path / "journal.db")
    fetch = FakeFetch({
        # AEHL gets a 3rd batch (the sharp breach); MSFT's queue ends at
        # 2 -- resync_all advances every watched symbol in lockstep, one
        # queued batch each, so giving MSFT nothing further to consume is
        # what actually proves it's undisturbed (a no-op catch_up, not
        # raced against AEHL's).
        "AEHL": [_entry_bars(), _ratchet_bars(), _sharp_breach_bar()],
        "MSFT": [_entry_bars(), _ratchet_bars()],
    })

    with _client(fetch, journal_store=store, symbol="AEHL") as c:
        c.post("/api/watch", data={"symbol": "MSFT"})
        assert _wait_until(lambda: all(
            (_sym(c, s) or {}).get("bar_count") == 15 for s in ("AEHL", "MSFT")
        ))
        _resync(c)  # ratchet, both symbols
        assert _wait_until(lambda: all(
            store.open_position_for(s) is not None for s in ("AEHL", "MSFT")
        ))
        msft_before = store.open_position_for("MSFT")

        # drive AEHL to its stop-out (its 3rd batch; MSFT's queue is
        # already exhausted, so this resync is a harmless no-op for it)
        _resync(c)
        assert store.open_position_for("AEHL") is None

        # MSFT must be completely unaffected: still open, identical values
        msft_after = store.open_position_for("MSFT")
        assert msft_after is not None
        assert msft_after.id == msft_before.id
        assert msft_after.entry_price == msft_before.entry_price == 9.1
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
        "AEHL": [_entry_bars()],
        "MSFT": [_entry_bars()],
    })

    with _client(fetch, journal_store=store, symbol="AEHL") as c:
        c.post("/api/watch", data={"symbol": "MSFT"})
        assert _wait_until(lambda: all(
            (_sym(c, s) or {}).get("bar_count") == 15 for s in ("AEHL", "MSFT")
        ))
        assert store.open_position_for("AEHL") is not None \
            and store.open_position_for("MSFT") is not None

        c.post("/api/unwatch", data={"symbol": "AEHL"})
        assert _wait_until(lambda: store.open_position_for("AEHL") is None)

        # MSFT's position must survive AEHL's removal untouched
        assert store.open_position_for("MSFT") is not None

    closed = store.recent_closed()
    assert len(closed) == 1
    assert closed[0]["symbol"] == "AEHL"


# -- Part B wiring: create_app's volume_confirm_threshold is honored end to end

def test_volume_confirm_threshold_wired_through_create_app_blocks_a_real_entry(tmp_path):
    # _entry_bars()'s real relative_volume (computed by core/indicators.py
    # from the fixture's flat per-bar volume) is 1.0 -- the same real
    # confirmed transition that fires cleanly through this file's default
    # (gate disabled, threshold=0.0) client must be BLOCKED once a real
    # threshold above 1.0 is wired all the way from create_app's own
    # kwarg through Poller down to journal_logic.should_enter.
    store = JournalStore(tmp_path / "journal.db")
    fetch = FakeFetch({"AEHL": [_entry_bars()]})

    with _client(fetch, journal_store=store, volume_confirm_threshold=1.5) as c:
        assert _wait_until(lambda: _sym(c, "AEHL").get("bar_count") == 15)
        time.sleep(0.1)
        assert store.open_position_for("AEHL") is None


# -- specs.md section 8: strategy_params are live-tunable, no restart -----

def test_changing_trail_pct_via_api_takes_effect_on_the_next_entry_no_restart(tmp_path):
    store = JournalStore(tmp_path / "journal.db",
                         default_params={"trail_pct": 0.05,
                                         "volume_confirm_threshold": 0.0})
    fetch = FakeFetch({"AEHL": [_entry_bars()]})

    with _client(fetch, journal_store=store) as c:
        assert _wait_until(lambda: _sym(c, "AEHL").get("bar_count") == 15)
        first = store.open_position_for("AEHL")
        assert first.trail_pct == 0.05
        assert first.stop_level == round(9.1 * (1 - 0.05), 10)

        # Change the LIVE value via the real API -- same process, no
        # restart -- then unwatch/re-watch to drive a second, independent
        # entry and confirm it picks up the new value immediately.
        r = c.post("/api/strategy_params", json={"trail_pct": 0.20})
        assert r.status_code == 200

        c.post("/api/unwatch", data={"symbol": "AEHL"})
        fetch2 = FakeFetch({"AEHL": [_entry_bars()]})
        with _client(fetch2, journal_store=store) as c2:
            assert _wait_until(lambda: _sym(c2, "AEHL").get("bar_count") == 15)
            second = store.open_position_for("AEHL")
            assert second.trail_pct == 0.20            # picked up the new value
            assert second.stop_level == round(9.1 * (1 - 0.20), 10)


def test_a_parameter_change_after_entry_does_not_affect_the_open_positions_ratchet(tmp_path):
    store = JournalStore(tmp_path / "journal.db",
                         default_params={"trail_pct": 0.05,
                                         "volume_confirm_threshold": 0.0})
    fetch = FakeFetch({"AEHL": [_entry_bars(), _ratchet_bars()]})

    with _client(fetch, journal_store=store) as c:
        assert _wait_until(lambda: _sym(c, "AEHL").get("bar_count") == 15)
        opened = store.open_position_for("AEHL")
        assert opened.trail_pct == 0.05

        # Change the live value WHILE this position is still open.
        r = c.post("/api/strategy_params", json={"trail_pct": 0.50})
        assert r.status_code == 200

        _resync(c)  # _ratchet_bars -- this position's own next decision
        ratcheted = store.open_position_for("AEHL")
        assert ratcheted.id == opened.id
        assert ratcheted.trail_pct == 0.05  # still the value locked in at ITS entry
        # high after ratchet_bars is 9.2 (see _ratchet_bars) -- stop must
        # reflect the LOCKED 0.05, not the new global 0.50, or this
        # assertion would read 9.2*0.5=4.6 instead.
        assert ratcheted.stop_level == round(9.2 * (1 - 0.05), 10)


# -- specs.md section 7: watch_note snapshotted onto the trade at entry ---

def test_a_real_entry_snapshots_the_note_current_at_that_moment(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    store.add_watch_note("AEHL", "halted on FDA news, watching for reclaim")
    fetch = FakeFetch({"AEHL": [_entry_bars()]})

    with _client(fetch, journal_store=store) as c:
        assert _wait_until(lambda: _sym(c, "AEHL").get("bar_count") == 15)
        opened = store.open_position_for("AEHL")
        assert opened.watch_note == "halted on FDA news, watching for reclaim"


def test_a_closed_trades_note_snapshot_is_unaffected_by_a_later_note_change(tmp_path):
    # The whole point of the feature: a trade's own row must answer "why
    # was I watching this" without cross-referencing watch_notes, which
    # can change (a re-watch, or an explicit update) after the fact.
    store = JournalStore(tmp_path / "journal.db")
    store.add_watch_note("AEHL", "original reason at entry")
    fetch = FakeFetch({"AEHL": [_entry_bars(), _ratchet_bars(), _sharp_breach_bar()]})

    with _client(fetch, journal_store=store) as c:
        assert _wait_until(lambda: _sym(c, "AEHL").get("bar_count") == 15)
        entered = store.open_position_for("AEHL")
        assert entered.watch_note == "original reason at entry"

        # The note changes WHILE this trade is still open.
        r = c.post("/api/watch_note", json={"symbol": "AEHL",
                                            "note": "a completely different later reason"})
        assert r.status_code == 200
        assert _sym(c, "AEHL").get("watch_note") == "a completely different later reason"

        _resync(c)  # ratchet
        _resync(c)  # sharp breach -- trailing stop trips, trade closes

    (closed,) = store.recent_closed()
    assert closed["id"] == entered.id
    assert closed["exit_reason"] == "trailing_stop"
    # the CLOSED trade's own snapshot is the ORIGINAL reason, untouched
    # by the note change that happened while it was still open
    assert closed["watch_note"] == "original reason at entry"
