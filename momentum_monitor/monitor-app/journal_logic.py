"""
journal_logic.py -- pure virtual-trade decision logic for the phase-4
virtual trade journal (specs.md section 6, "Virtual trade journal").

No I/O, no SQLite, no network -- mirrors momentum_monitor/core's own
"pure logic separated from I/O" boundary (see core/indicators.py's
docstring: analysis logic tangled into a UI/network layer is exactly what
made ToS_Companion untestable). This module lives in monitor-app rather
than core/ because it depends on Poller-level state (which symbol is
watched, the accumulated bar list) rather than being pure market
analysis -- but the same "no I/O" discipline applies.

Reuses core/setup_types.py's evaluate_setups() output (the `setups` list
already computed by state.build_state and already displayed on the page,
each with its own `hold.confirmed` boolean) -- this module invents no new
entry-signal logic of its own. The asymmetry between entry and exit is
the SAME one specs.md section 3 already documents as a non-negotiable
principle for hold-confirmation generally: entries require sustained
confirmation, stops fire immediately and unconditionally. Applying that
same asymmetry to this journal's entries/exits is a continuation of an
existing rule, not a new one.

Entry (generalized 2026-09-17 -- see specs.md; originally fired ONLY on
the nearest-above resistance level, written before phase 3.5's
multi-scenario evaluation existed and never generalized afterward):
fires on ANY of the four setup types' (resistance breakout, micro
breakout, VWAP pullback-reclaim, round-number reclaim) own
hold_confirmed False->True transition, tracked independently PER TYPE
(`was_confirmed_types`/`confirmed_types_after` -- a set of setup_type
strings, not one collapsed boolean) -- "closest" is setup_types.py's own
comparison metric for what to show/watch, not a requirement for a
confirmation to count as a real entry signal. If more than one type
transitions in the same tick, the closest (setups is pre-sorted
ascending by distance, per evaluate_setups' own contract) wins -- a
deterministic tie-break, not an arbitrary one. Also gated by volume
(added 2026-09-17): `relative_volume` must clear
`volume_confirm_threshold` AT THE MOMENT OF CONFIRMATION, same as
`hold.confirmed` itself -- a type that confirms on unremarkable volume
does not fire, and does not get a second chance later while it stays
confirmed (see `_first_newly_confirmed` below: the type is still marked
"seen" whether or not the volume gate let it fire, since nothing about
it has changed if it's still sitting at the same confirmed state next
tick). Still fires only when no position is already open for the
symbol. entry_price is the close of the bar the transition is observed
at (the finest granularity available without re-running evaluate_hold
per-bar inside a single poll cycle, which would be inventing new entry
logic -- see advance_journal below for how a poll's whole batch of new
bars is handled). The specific setup_type and factors (distance,
trigger_price, relative_volume, plus whatever type-specific detail
setup_types.py's own SetupCandidate.factors carries) are captured on
the OpenPosition at the moment of entry, not re-derived later from
whatever happens to be displayed at review time.

Exit: TRAIL_PCT (a starting point to tune against real logged data, not
a validated number -- see main.py) below the running high_water_mark,
which only ever ratchets up (from each bar's HIGH, never its close) and
never moves down. A bar's LOW crossing below the current stop_level
exits immediately, no confirmation delay -- deliberately mirroring
specs.md section 3's existing asymmetry, not a new invention. Exits are
deliberately NOT volume-gated -- that asymmetry (entries need sustained
confirmation and, now, real volume; stops fire fast and unconditionally,
no exceptions) has been the rule since core/ was first built, and
applies here too, not just to hold-confirmation. A fixed R:R target was
explicitly rejected for this project (it capped winners in the EOD swing
bot and contributed to that strategy's edge not holding up under
testing) -- there is no target anywhere in this module, by design, not
by omission.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class OpenPosition:
    id: int | None
    symbol: str
    entry_ts: int
    entry_price: float
    high_water_mark: float
    stop_level: float
    # Which of the four setup types fired, and the factors behind it at
    # the moment of entry (added 2026-09-17, see specs.md) -- so "why did
    # this trade happen" is answerable later without guessing from
    # whatever's currently on screen. None for positions opened before
    # this existed.
    setup_type: str | None = None
    factors: dict | None = None
    # trail_pct LOCKED IN at entry from the live-tunable strategy_params
    # value in effect at that moment (added 2026-09-18, see specs.md
    # section 8) -- apply_bar_to_open_position always reads THIS field,
    # never a freshly-looked-up global, so a parameter change made while
    # this position is open never affects it; only a NEW entry picks up
    # the new value. volume_threshold_used is the matching snapshot for
    # entry's own volume gate (exits are never volume-gated, so it has
    # no ongoing use after entry -- captured for the trade record only).
    trail_pct: float = 0.05
    volume_threshold_used: float | None = None
    # The watch_notes entry current for this symbol AT THE MOMENT of
    # entry (added 2026-09-18, specs.md section 7's highest-priority
    # gap) -- SNAPSHOTTED, same principle as trail_pct above, never a
    # live reference to journal_store.current_note_for(). The symbol
    # could be re-watched with different context, or the note updated
    # again, before anyone reviews this trade; this field must keep
    # answering "why was I watching this" regardless. None if no note
    # was ever recorded for this symbol before this entry.
    watch_note: str | None = None
    # Position sizing, computed ONCE at entry from the live current_equity/
    # risk_pct_per_trade values in effect at that exact moment (added
    # 2026-09-18, specs.md section 7's position-sizing gap) -- same
    # snapshot-at-entry discipline as trail_pct/watch_note above, and for
    # the same reason: current_equity keeps moving as later trades close,
    # so re-deriving these later from "whatever current_equity is now"
    # would misattribute a trade's own sizing to a value it never actually
    # used. account_size_used is the current_equity reading itself;
    # risk_pct_used is the risk_pct_per_trade reading; shares is
    # floor(risk_amount / risk_per_share), which can be 0 (an expensive
    # stock, a tight stop, or a small current_equity) -- a real, valid
    # outcome that still gets journaled, not suppressed, so shares is
    # never a fallback/sentinel value, only the true computed count.
    # risk_amount_used is the ACTUAL dollar amount the rounded share count
    # risks (shares * risk_per_share), which can differ slightly from the
    # theoretical risk_amount target -- the real number is recorded, not
    # the theoretical one. All four None for a position opened before this
    # existed (migrated in place, see journal_store.py) or built directly
    # in a test/tool without sizing inputs -- never a numeric sentinel
    # that could be mistaken for a real computed value (0 shares IS a
    # real, meaningful value; None means "never computed" instead).
    shares: int | None = None
    account_size_used: float | None = None
    risk_pct_used: float | None = None
    risk_amount_used: float | None = None


@dataclass(frozen=True)
class ExitEvent:
    exit_ts: int
    exit_price: float
    exit_reason: str  # "trailing_stop" | "symbol_switched"


@dataclass(frozen=True)
class JournalTick:
    """The result of running one poll cycle's newly-arrived bars through
    the journal decision logic. The caller (Poller, which owns the
    SQLite-backed journal_store) applies whichever of these actually
    happened -- this function performs no I/O itself."""
    opened: OpenPosition | None = None
    updated: OpenPosition | None = None   # ratcheted, still open
    closed: tuple[OpenPosition, ExitEvent] | None = None
    # Every setup_type currently confirmed, tracked per-type (not one
    # collapsed boolean) so a DIFFERENT type confirming later, while
    # another type is still sitting confirmed from earlier, is still
    # detectable as its own fresh transition -- see should_enter.
    confirmed_types_after: frozenset[str] = frozenset()


def initial_stop_level(entry_price: float, trail_pct: float) -> float:
    return entry_price * (1 - trail_pct)


def _first_newly_confirmed(setups: list[dict],
                           was_confirmed_types: frozenset[str]) -> dict | None:
    """The first (closest -- setups is pre-sorted ascending by distance,
    per setup_types.evaluate_setups' own contract) setup whose type just
    transitioned hold.confirmed False->True. None if none did. A type
    that's confirmed but was ALSO confirmed last tick doesn't count --
    that's not a fresh transition, it's the same one continuing."""
    for s in setups:
        if s["hold"]["confirmed"] and s["setup_type"] not in was_confirmed_types:
            return s
    return None


def should_enter(*, newly_confirmed_type: str | None, relative_volume: float,
                 volume_confirm_threshold: float, position_open: bool) -> bool:
    """True when some setup type freshly transitioned to confirmed (any
    of the four -- generalized 2026-09-17, see module docstring),
    relative_volume clears volume_confirm_threshold at that same moment,
    and no position is already open. This is the entire entry rule --
    exits (apply_bar_to_open_position) are deliberately NOT volume-gated,
    the same entry/exit asymmetry core/ has always used."""
    return (newly_confirmed_type is not None and not position_open
            and relative_volume >= volume_confirm_threshold)


def apply_bar_to_open_position(
    position: OpenPosition, bar: dict,
) -> tuple[OpenPosition, ExitEvent | None]:
    """Ratchet high_water_mark up from this bar's high (never down), then
    check this bar's low against the freshly-ratcheted stop_level --
    checking the RATCHETED value, not the pre-bar one, is deliberate: OHLC
    bars don't record whether the high or the low happened first, so the
    worse-case-for-the-position ordering is assumed, consistent with
    "stops fire fast, no exceptions." Returns the updated position and an
    ExitEvent if the stop was breached this bar, else None.

    Always uses `position.trail_pct` -- the value locked in at THIS
    position's own entry (2026-09-18: strategy_params is live-tunable,
    but an open position's trail_pct is deliberately NOT re-read from the
    current global on every bar, so a parameter change mid-trade can
    never move an already-open position's stop math; see specs.md
    section 8) -- never a separately-passed value.

    exit_price on a breach is the stop_level itself, not the bar's low --
    a virtual/simulated-fill modeling choice (assume the stop fills at the
    stop price), not a claim about real fill behavior."""
    new_hwm = max(position.high_water_mark, bar["high"])
    new_stop = initial_stop_level(new_hwm, position.trail_pct)
    updated = replace(position, high_water_mark=new_hwm, stop_level=new_stop)
    if bar["low"] < new_stop:
        return updated, ExitEvent(exit_ts=bar["ts"], exit_price=new_stop,
                                  exit_reason="trailing_stop")
    return updated, None


def advance_journal(
    *, position: OpenPosition | None, new_bars: list[dict],
    setups: list[dict], was_confirmed_types: frozenset[str],
    relative_volume: float, volume_confirm_threshold: float,
    trail_pct: float, symbol: str, current_equity: float,
    risk_pct_per_trade: float, watch_note: str | None = None,
) -> JournalTick:
    """Run one poll cycle's newly-arrived bars (in order) through the
    journal: if a position is open, walk each new bar ratcheting the stop
    and checking for a breach (stopping at the first breach -- bars after
    an exit within the same batch are not evaluated for a fresh entry;
    that waits for the NEXT independent confirmation transition, per
    should_enter's "only one open position at a time" rule). Then, if no
    position remains open, check for a fresh entry using the poll's final
    (already fully-recomputed, per state.build_state) `setups` list --
    ANY of the four types, not just resistance (see module docstring).

    `setups` is state.build_state's own "setups" value: a list of dicts
    shaped like setup_types.SetupCandidate (setup_type, trigger_price,
    distance, hold, factors), already sorted ascending by distance.
    `relative_volume` is that same state's session.relative_volume.

    `trail_pct`/`volume_confirm_threshold` here are the CURRENT live
    strategy_params values (2026-09-18, see specs.md section 8) -- used
    ONLY to price a brand-new entry and get locked onto it
    (OpenPosition.trail_pct/volume_threshold_used). An already-open
    `position` ratchets using ITS OWN locked-in trail_pct
    (apply_bar_to_open_position reads position.trail_pct, not this
    parameter) -- a mid-trade parameter change never reaches it.

    `current_equity`/`risk_pct_per_trade` (2026-09-18, specs.md section 7's
    position-sizing gap) are likewise the CURRENT live values, read by the
    caller at the literal moment this function is invoked -- used ONLY to
    size a brand-new entry (never re-read for an already-open position;
    apply_bar_to_open_position takes neither). Required, not optional
    (no default), same treatment as trail_pct -- sizing math has no
    meaningful zero-effort default the way an optional watch_note does.
    """
    current = position
    updated = None
    closed = None

    for bar in new_bars:
        if current is None:
            break
        current, exit_event = apply_bar_to_open_position(current, bar)
        if exit_event is not None:
            closed = (current, exit_event)
            current = None
            updated = None  # mutually exclusive with `closed` -- a caller
            # applying tick results must never see both a still-open
            # "updated" position and a "closed" one for the same tick.
            break
        updated = current

    # Always reflects the CURRENT confirmed set, regardless of whether an
    # entry fired or was blocked by the volume gate below -- a type that
    # confirmed but didn't clear volume doesn't get re-checked every tick
    # it stays confirmed; that's not a fresh transition happening again,
    # it's the same one still sitting there.
    confirmed_types_now = frozenset(
        s["setup_type"] for s in setups if s["hold"]["confirmed"]
    )

    opened = None
    if current is None and new_bars:
        candidate = _first_newly_confirmed(setups, was_confirmed_types)
        newly_type = candidate["setup_type"] if candidate is not None else None
        if should_enter(
            newly_confirmed_type=newly_type, relative_volume=relative_volume,
            volume_confirm_threshold=volume_confirm_threshold,
            position_open=False,
        ):
            entry_bar = new_bars[-1]
            entry_price = entry_bar["close"]
            # risk_per_share is the dollar distance from entry to the
            # initial stop (entry_price * trail_pct, algebraically the
            # same distance initial_stop_level computes below) -- shares
            # is how many of those risk_per_share units fit inside this
            # entry's risk budget, rounded DOWN (never up: overshooting
            # risk_amount on a rounding technicality would defeat the
            # whole point of a risk-based size). risk_amount_used is the
            # REAL amount the rounded share count risks, which can differ
            # slightly from the theoretical risk_amount target above --
            # the real number is what gets recorded.
            risk_amount = current_equity * risk_pct_per_trade
            risk_per_share = entry_price * trail_pct
            shares = math.floor(risk_amount / risk_per_share) if risk_per_share > 0 else 0
            risk_amount_used = shares * risk_per_share
            opened = OpenPosition(
                id=None, symbol=symbol, entry_ts=entry_bar["ts"],
                entry_price=entry_price, high_water_mark=entry_price,
                stop_level=initial_stop_level(entry_price, trail_pct),
                setup_type=candidate["setup_type"],
                factors={
                    **candidate["factors"],
                    "distance": candidate["distance"],
                    "trigger_price": candidate["trigger_price"],
                    "relative_volume": relative_volume,
                },
                trail_pct=trail_pct,
                volume_threshold_used=volume_confirm_threshold,
                watch_note=watch_note,
                shares=shares, account_size_used=current_equity,
                risk_pct_used=risk_pct_per_trade, risk_amount_used=risk_amount_used,
            )

    return JournalTick(opened=opened, updated=updated, closed=closed,
                       confirmed_types_after=confirmed_types_now)
