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

import pytest
from fastapi.testclient import TestClient

from app import create_app
from journal_logic import initial_stop_level
from journal_store import JournalStore

TRAIL_PCT = 0.05
SWING_LOW_BUFFER_PCT = 0.005  # app.py's DEFAULT_SWING_LOW_BUFFER_PCT


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
    little without breaching the OLD flat-trail-only stop (8.645) or
    producing any new confirmation. Still used below by tests that don't
    care exactly which batch triggers an eventual close (they only check
    "did it eventually close with the right reason"); NOT safe against
    phase 1's own, deliberately tighter, early-phase stop (specs.md
    section 12) -- see _phase1_safe_ratchet_bars for that."""
    return [_bar(150, 9.15), _bar(160, 9.05)]


def _phase1_safe_ratchet_bars():
    """Two bars that raise high_water_mark a little while staying safely
    above phase 1's OWN stop (initial_stop_level(9.1, SWING_LOW_BUFFER_
    PCT) = 9.0545, see specs.md section 12) -- for tests that need the
    position to survive a ratchet without closing, for reasons unrelated
    to the two-phase exit itself (duplicate-entry guarding, cross-symbol
    independence). Also stays BELOW the phase 2 progress threshold
    (9.1*1.03=9.373), so the position stays in phase 1 throughout, same
    as _ratchet_bars() was meant to represent under the old design."""
    return [_bar(150, 9.20), _bar(160, 9.25)]


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
           volume_confirm_threshold=0.0, fetch_daily_bars=None):
    # Threshold defaults to 0.0 (always clears) -- this file proves the
    # Poller<->journal_logic<->journal_store WIRING against real bar-
    # driven hold_confirmed transitions, not the volume gate itself
    # (test_journal_logic.py's advance_journal tests own that; these
    # fixtures' flat per-bar volume produces relative_volume ~= 1.0,
    # which is a real "no genuine spike" reading, not a fixture bug --
    # gating it off here keeps that concern out of the wiring tests it
    # would otherwise silently couple to). fetch_daily_bars defaults to
    # None, same "skip the session-level volume gate" treatment.
    app = create_app(fetch_bars=fetch, watch_symbol=symbol,
                     announce_watch=announce, announce_unwatch=unwatch,
                     announce_retry_attempts=1,
                     journal_store=journal_store, trail_pct=trail_pct,
                     volume_confirm_threshold=volume_confirm_threshold,
                     fetch_daily_bars=fetch_daily_bars,
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
    # Phase 1's own anchor (specs.md section 12), not trail_pct-derived:
    # round_number_reclaim's trigger_price here (9.25) exceeds entry_price
    # (9.1) -- confirmed directly against real setup_types output -- so
    # _phase1_anchor clamps the anchor to entry_price itself.
    assert pos.stop_level == initial_stop_level(9.1, SWING_LOW_BUFFER_PCT)
    assert pos.exit_phase == "swing_low"
    assert pos.setup_type == "round_number_reclaim"


def test_entry_does_not_duplicate_on_subsequent_confirmed_polls(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    fetch = FakeFetch({"AEHL": [_entry_bars(), _phase1_safe_ratchet_bars()]})

    with _client(fetch, journal_store=store) as c:
        assert _wait_until(lambda: _sym(c, "AEHL").get("bar_count") == 15)
        entered = store.open_position_for("AEHL")
        _resync(c)  # _phase1_safe_ratchet_bars -- make sure no second entry sneaks in
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
        "AEHL": [_entry_bars(), _phase1_safe_ratchet_bars(), _sharp_breach_bar()],
        "MSFT": [_entry_bars(), _phase1_safe_ratchet_bars()],
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
        # Entry-time stop_level is phase 1's anchor (specs.md section 12),
        # NOT trail_pct-derived anymore -- trail_pct is still locked onto
        # the position (checked above) for when phase 2 eventually takes
        # over, but doesn't drive the stop at entry itself.
        assert first.stop_level == initial_stop_level(9.1, SWING_LOW_BUFFER_PCT)

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
            # Still phase 1's formula (trail_pct doesn't govern entry
            # stop_level either way) -- unaffected by the trail_pct change.
            assert second.stop_level == initial_stop_level(9.1, SWING_LOW_BUFFER_PCT)


def test_a_parameter_change_after_entry_does_not_affect_the_open_positions_ratchet(tmp_path):
    # trail_pct only governs the stop once phase 2 ("trailing") has taken
    # over (specs.md section 12) -- so THIS test (trail_pct isolation)
    # needs the position actually in phase 2 to mean anything; a
    # dedicated transition batch (high water mark clearing entry_price *
    # 1.03, the default pattern_progress_threshold_pct) does that first.
    store = JournalStore(tmp_path / "journal.db",
                         default_params={"trail_pct": 0.05,
                                         "volume_confirm_threshold": 0.0})
    transition_batch = [_bar(150, 9.50)]  # high=9.55 clears 9.1*1.03=9.373
    post_change_batch = [_bar(160, 9.60)]  # a further, ordinary ratchet
    fetch = FakeFetch({"AEHL": [_entry_bars(), transition_batch, post_change_batch]})

    with _client(fetch, journal_store=store) as c:
        assert _wait_until(lambda: _sym(c, "AEHL").get("bar_count") == 15)
        opened = store.open_position_for("AEHL")
        assert opened.trail_pct == 0.05
        assert opened.exit_phase == "swing_low"

        _resync(c)  # transition_batch -- pushes into phase 2
        transitioned = store.open_position_for("AEHL")
        assert transitioned is not None
        assert transitioned.exit_phase == "trailing"

        # Change the live value WHILE this position is still open, now
        # that it's actually in the phase trail_pct governs.
        r = c.post("/api/strategy_params", json={"trail_pct": 0.50})
        assert r.status_code == 200

        _resync(c)  # post_change_batch -- this position's own next decision
        ratcheted = store.open_position_for("AEHL")
        assert ratcheted.id == opened.id
        assert ratcheted.trail_pct == 0.05  # still the value locked in at ITS entry
        # high after post_change_batch is 9.65 -- stop must reflect the
        # LOCKED 0.05, not the new global 0.50, or this assertion would
        # read 9.65*0.5=4.825 instead.
        assert ratcheted.stop_level == round(9.65 * (1 - 0.05), 10)


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


# -- position sizing with compounding virtual equity, specs.md section 7 --

# Real numbers for _entry_bars()+_sharp_breach_bar() under this file's
# default current_equity (2000.0, the journal_store seed fallback -- no
# default_params passed by _client below) and risk_pct_per_trade (0.01,
# app.py's DEFAULT_RISK_PCT_PER_TRADE, since journal_store has no row for
# it either): risk_amount = 2000*0.01 = 20.0; risk_per_share =
# 9.1*TRAIL_PCT(0.05) = 0.455 (sizing always uses trail_pct, independent
# of which phase actually prices the stop -- specs.md section 12); shares
# = floor(20.0/0.455) = 43; risk_amount_used = 43*0.455 = 19.565.
#
# The breach itself, though, is now governed by PHASE 1's stop (specs.md
# section 12), not the flat trail: round_number_reclaim's trigger_price
# (9.25) exceeds entry_price (9.1) here -- confirmed directly against
# real setup_types output -- so _phase1_anchor clamps the anchor to
# entry_price itself. No swing low confirms and no ratchet/transition
# happens before the sharp breach (only 1 bar arrives since entry, far
# short of the window=3 confirmation minimum, and the breach bar's own
# high (8.6) never clears entry_price), so the stop stays fixed at
# initial_stop_level(9.1, app.py's DEFAULT_SWING_LOW_BUFFER_PCT=0.005) =
# 9.1*0.995 = 9.0545 the whole time -- a much TIGHTER stop than the flat
# trail's 8.645 would have been, by design (phase 1 exists to cut a
# failing pattern early, not ride it down 5%). Loss: 43*(9.0545-9.1) =
# -1.9565.
_EXPECTED_SHARES = 43
_EXPECTED_RISK_AMOUNT_USED = 43 * (9.1 * TRAIL_PCT)
_EXPECTED_PHASE1_STOP = initial_stop_level(9.1, SWING_LOW_BUFFER_PCT)
_EXPECTED_LOSS_DOLLARS = 43 * (_EXPECTED_PHASE1_STOP - 9.1)


def test_entry_sizing_is_computed_and_persisted_end_to_end(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    fetch = FakeFetch({"AEHL": [_entry_bars()]})
    with _client(fetch, journal_store=store) as c:
        assert _wait_until(lambda: _sym(c, "AEHL").get("bar_count") == 15)
        pos = store.open_position_for("AEHL")

    assert pos.shares == _EXPECTED_SHARES
    assert pos.account_size_used == 2000.0
    assert pos.risk_pct_used == 0.01
    assert pos.risk_amount_used == pytest.approx(_EXPECTED_RISK_AMOUNT_USED)


def test_closed_trade_realized_pnl_dollars_and_compounded_equity_end_to_end(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    fetch = FakeFetch({"AEHL": [_entry_bars(), _sharp_breach_bar()]})
    with _client(fetch, journal_store=store) as c:
        assert _wait_until(lambda: _sym(c, "AEHL").get("bar_count") == 15)
        _resync(c)  # sharp breach -- trailing stop trips, trade closes
        assert store.open_position_for("AEHL") is None

    (closed,) = store.recent_closed()
    assert closed["realized_pnl_dollars"] == pytest.approx(_EXPECTED_LOSS_DOLLARS)
    assert store.current_equity() == pytest.approx(2000.0 + _EXPECTED_LOSS_DOLLARS)

    history = store.equity_history()
    assert len(history) == 1
    assert history[0]["reason"].startswith(f"trade_close:trade_id={closed['id']}:")
    assert history[0]["old_value"] == pytest.approx(2000.0)
    assert history[0]["new_value"] == pytest.approx(2000.0 + _EXPECTED_LOSS_DOLLARS)


def test_symbol_switched_force_close_does_not_move_current_equity(tmp_path):
    # Housekeeping, not a trading outcome (specs.md section 6 -- muted
    # display, excluded from win/loss, bulk-deletable) -- unwatching a
    # symbol with an open position must never silently move the virtual
    # account balance off whatever price happened to be current at that
    # moment. The row still records a real, honest realized_pnl_dollars
    # (the fact of what the price move WOULD have been), it just never
    # reaches current_equity -- see app.py's _update_journal vs.
    # remove_symbol, the only two callers of close_position.
    store = JournalStore(tmp_path / "journal.db")
    fetch = FakeFetch({"AEHL": [_entry_bars()]})
    with _client(fetch, journal_store=store) as c:
        assert _wait_until(lambda: _sym(c, "AEHL").get("bar_count") == 15)
        entered = store.open_position_for("AEHL")
        assert entered.shares == _EXPECTED_SHARES
        c.post("/api/unwatch", data={"symbol": "AEHL"})
        assert _wait_until(lambda: store.open_position_for("AEHL") is None)

    (closed,) = store.recent_closed()
    assert closed["exit_reason"] == "symbol_switched"
    assert closed["realized_pnl_dollars"] is not None
    assert store.current_equity() == 2000.0       # UNCHANGED
    assert store.equity_history() == []            # no trade_close entry at all


def test_two_symbols_closing_in_the_same_resync_batch_compound_equity_sequentially(tmp_path):
    # The concurrency requirement (specs.md section 7): two REAL trade
    # closes, for two DIFFERENT symbols, landing within the SAME
    # resync_all() call -- the real "same processing batch/poll cycle"
    # shape in this codebase: resync_all() awaits catch_up() for every
    # watched symbol in turn, all before the caller (here, _resync's
    # POST /api/polling round trip) gets a response. Proves current_
    # equity compounds BOTH realized P&L amounts correctly and
    # sequentially -- neither lost nor double-applied. This is the same
    # CLASS of bug already found once in this project (app.py's
    # opened/updated/closed if/elif/elif silently dropping a write when
    # two things happened in the same batch, Part A of setup-type
    # generalization, specs.md) in a new location: current_equity's own
    # update path, not the trades table.
    store = JournalStore(tmp_path / "journal.db")
    fetch = FakeFetch({
        "AEHL": [_entry_bars(), _sharp_breach_bar()],
        "MSFT": [_entry_bars(), _sharp_breach_bar()],
    })

    with _client(fetch, journal_store=store, symbol="AEHL") as c:
        assert _wait_until(lambda: _sym(c, "AEHL").get("bar_count") == 15)
        c.post("/api/watch", data={"symbol": "MSFT"})
        assert _wait_until(lambda: _sym(c, "MSFT").get("bar_count") == 15)

        aehl_entry = store.open_position_for("AEHL")
        msft_entry = store.open_position_for("MSFT")
        assert aehl_entry.shares == _EXPECTED_SHARES
        assert msft_entry.shares == _EXPECTED_SHARES
        # Both entries sized off the SAME starting equity -- neither
        # symbol has closed yet at either one's own moment of entry.
        assert aehl_entry.account_size_used == 2000.0
        assert msft_entry.account_size_used == 2000.0

        # ONE resync_all() call processes AEHL then MSFT in turn
        # (insertion order == watch order) -- both close within it.
        _resync(c)

        assert store.open_position_for("AEHL") is None
        assert store.open_position_for("MSFT") is None

    assert len(store.recent_closed(limit=10)) == 2
    assert store.current_equity() == pytest.approx(2000.0 + 2 * _EXPECTED_LOSS_DOLLARS)

    history = store.equity_history()
    assert len(history) == 2
    # Sequential, not lost or double-applied: the SECOND close (most
    # recent, history[0]) applied against the equity the FIRST close had
    # ALREADY moved -- never the original 2000.0 twice over, and never
    # just one of the two losses alone.
    assert history[0]["old_value"] == pytest.approx(2000.0 + _EXPECTED_LOSS_DOLLARS)
    assert history[0]["new_value"] == pytest.approx(2000.0 + 2 * _EXPECTED_LOSS_DOLLARS)
    assert history[1]["old_value"] == pytest.approx(2000.0)
    assert history[1]["new_value"] == pytest.approx(2000.0 + _EXPECTED_LOSS_DOLLARS)


# -- swing-low-anchored early-phase stop, wired end to end (specs.md
# section 12) -- proves core/levels.confirmed_swing_lows is correctly
# called with the right bars (the position's own history since entry)
# and its result correctly reprices the stop, through the REAL Poller
# pipeline, not just journal_logic.py's own hand-crafted-position tests.

def _post_entry_swing_low_bars():
    """7 bars after a real entry at 9.1 (round_number_reclaim, see
    _entry_bars) forming a clean V dipping BELOW entry_price -- a
    genuine pullback-then-recovery, verified by direct experiment
    against core/levels.confirmed_swing_lows: a confirmed swing low at
    price 9.07 (the low of the center bar, ts=180). Deliberately below
    entry_price (9.1), not above it: a candidate low ABOVE entry_price
    gets clamped back down to entry_price by _phase1_anchor (found
    while writing this very test -- the clamp working exactly as
    designed, not a bug, but it means an above-entry swing low is
    numerically indistinguishable from the entry-trigger fallback, so
    it can't prove the anchor actually changed). 9.07 stays safely
    above the entry-time phase-1 stop (initial_stop_level(9.1,
    0.005)=9.0545) so the position survives the whole batch, and highs
    stay under entry_price*1.03=9.373 so phase 2's progress threshold
    never fires -- isolating the swing-low-anchor mechanism from the
    phase transition, which test_a_parameter_change_after_entry_does_
    not_affect_the_open_positions_ratchet already covers separately."""
    return [_bar(150, 9.20), _bar(160, 9.17), _bar(170, 9.14), _bar(180, 9.12),
           _bar(190, 9.14), _bar(200, 9.17), _bar(210, 9.20)]


def test_a_real_confirmed_swing_low_reprices_the_phase1_stop(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    fetch = FakeFetch({"AEHL": [_entry_bars(), _post_entry_swing_low_bars()]})

    with _client(fetch, journal_store=store) as c:
        assert _wait_until(lambda: _sym(c, "AEHL").get("bar_count") == 15)
        entered = store.open_position_for("AEHL")
        assert entered.exit_phase == "swing_low"
        assert entered.stop_level == initial_stop_level(9.1, SWING_LOW_BUFFER_PCT)

        _resync(c)  # the 7-bar V -- a real swing low confirms within it
        assert _sym(c, "AEHL")["bar_count"] == 22

    reanchored = store.open_position_for("AEHL")
    assert reanchored is not None      # survived the whole batch, no breach
    assert reanchored.id == entered.id
    assert reanchored.exit_phase == "swing_low"  # still phase 1
    # Reflects the REAL confirmed swing low (9.07), not the entry-time
    # trigger-price fallback (9.1) anymore.
    assert reanchored.stop_level == pytest.approx(
        initial_stop_level(9.07, SWING_LOW_BUFFER_PCT))
    assert reanchored.stop_level != initial_stop_level(9.1, SWING_LOW_BUFFER_PCT)


# -- session-level volume gate, wired end to end (specs.md section 12) ----
# _entry_bars() is 15 bars at 50_000 volume each (see _bar's default) --
# session_cumulative_volume by entry time is a REAL, computed 750_000
# (state.py's actual session slice, not simulated).

def _daily_bars_fetcher(avg_volume):
    async def fetch_daily_bars(symbol):
        return [{"ts": i, "volume": avg_volume, "open": 1, "high": 1,
                 "low": 1, "close": 1, "is_extended": False} for i in range(5)]
    return fetch_daily_bars


def test_session_volume_gate_blocks_a_real_entry_when_avg_daily_volume_too_high(tmp_path):
    # avg_daily_volume=1_000_000 needs session_cumulative_volume >=
    # 3_000_000 (the default session_volume_multiple=3.0) to pass --
    # 750_000 falls far short, so the real hold_confirmed transition
    # fires but the entry itself is blocked, same as a failed bar-level
    # volume_confirm_threshold check.
    store = JournalStore(tmp_path / "journal.db")
    fetch = FakeFetch({"AEHL": [_entry_bars()]})

    with _client(fetch, journal_store=store,
                fetch_daily_bars=_daily_bars_fetcher(1_000_000.0)) as c:
        assert _wait_until(lambda: _sym(c, "AEHL").get("bar_count") == 15)
        assert _sym(c, "AEHL").get("avg_daily_volume") == 1_000_000.0

    assert store.open_position_for("AEHL") is None
    assert store.recent_closed() == []


def test_session_volume_gate_allows_a_real_entry_when_session_volume_clears_it(tmp_path):
    # avg_daily_volume=100_000 needs session_cumulative_volume >= 300_000
    # -- 750_000 clears it comfortably, so the real entry fires.
    store = JournalStore(tmp_path / "journal.db")
    fetch = FakeFetch({"AEHL": [_entry_bars()]})

    with _client(fetch, journal_store=store,
                fetch_daily_bars=_daily_bars_fetcher(100_000.0)) as c:
        assert _wait_until(lambda: _sym(c, "AEHL").get("bar_count") == 15)
        assert _wait_until(lambda: store.open_position_for("AEHL") is not None)

    pos = store.open_position_for("AEHL")
    assert pos is not None
    # Snapshotted at entry (specs.md section 12), same "why did this
    # trade happen" discipline as relative_volume.
    assert pos.factors["avg_daily_volume"] == 100_000.0
    assert pos.factors["session_cumulative_volume"] == pytest.approx(750_000.0)
    assert pos.factors["session_volume_multiple_used"] == 3.0
