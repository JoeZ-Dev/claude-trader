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

Reuses core/levels.py's evaluate_hold output (the `hold.confirmed`
boolean already computed by state.build_state and already displayed on
the page) -- this module invents no new entry-signal logic of its own.
The asymmetry between entry and exit is the SAME one specs.md section 3
already documents as a non-negotiable principle for hold-confirmation
generally: entries require sustained confirmation, stops fire immediately
and unconditionally. Applying that same asymmetry to this journal's
entries/exits is a continuation of an existing rule, not a new one.

Entry: fires exactly once per hold_confirmed False->True transition on
the nearest-above resistance level, and only when no position is already
open. entry_price is the close of the bar the transition is observed at
(the finest granularity available without re-running evaluate_hold
per-bar inside a single poll cycle, which would be inventing new entry
logic -- see advance_journal below for how a poll's whole batch of new
bars is handled).

Exit: TRAIL_PCT (a starting point to tune against real logged data, not
a validated number -- see main.py) below the running high_water_mark,
which only ever ratchets up (from each bar's HIGH, never its close) and
never moves down. A bar's LOW crossing below the current stop_level
exits immediately, no confirmation delay -- deliberately mirroring
specs.md section 3's existing asymmetry, not a new invention. A fixed
R:R target was explicitly rejected for this project (it capped winners
in the EOD swing bot and contributed to that strategy's edge not holding
up under testing) -- there is no target anywhere in this module, by
design, not by omission.
"""
from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class OpenPosition:
    id: int | None
    symbol: str
    entry_ts: int
    entry_price: float
    high_water_mark: float
    stop_level: float


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
    was_confirmed_after: bool = False


def initial_stop_level(entry_price: float, trail_pct: float) -> float:
    return entry_price * (1 - trail_pct)


def should_enter(*, was_confirmed_before: bool, is_confirmed_now: bool,
                 position_open: bool) -> bool:
    """True exactly on a False->True hold_confirmed transition, and only
    when no position is already open. This is the entire entry rule."""
    return is_confirmed_now and not was_confirmed_before and not position_open


def apply_bar_to_open_position(
    position: OpenPosition, bar: dict, trail_pct: float,
) -> tuple[OpenPosition, ExitEvent | None]:
    """Ratchet high_water_mark up from this bar's high (never down), then
    check this bar's low against the freshly-ratcheted stop_level --
    checking the RATCHETED value, not the pre-bar one, is deliberate: OHLC
    bars don't record whether the high or the low happened first, so the
    worse-case-for-the-position ordering is assumed, consistent with
    "stops fire fast, no exceptions." Returns the updated position and an
    ExitEvent if the stop was breached this bar, else None.

    exit_price on a breach is the stop_level itself, not the bar's low --
    a virtual/simulated-fill modeling choice (assume the stop fills at the
    stop price), not a claim about real fill behavior."""
    new_hwm = max(position.high_water_mark, bar["high"])
    new_stop = initial_stop_level(new_hwm, trail_pct)
    updated = replace(position, high_water_mark=new_hwm, stop_level=new_stop)
    if bar["low"] < new_stop:
        return updated, ExitEvent(exit_ts=bar["ts"], exit_price=new_stop,
                                  exit_reason="trailing_stop")
    return updated, None


def advance_journal(
    *, position: OpenPosition | None, new_bars: list[dict],
    is_confirmed_now: bool, was_confirmed_before: bool,
    trail_pct: float, symbol: str,
) -> JournalTick:
    """Run one poll cycle's newly-arrived bars (in order) through the
    journal: if a position is open, walk each new bar ratcheting the stop
    and checking for a breach (stopping at the first breach -- bars after
    an exit within the same batch are not evaluated for a fresh entry;
    that waits for the NEXT independent hold_confirmed transition, per
    should_enter's "only one open position at a time" rule). Then, if no
    position remains open, check for a fresh entry using the poll's final
    (already fully-recomputed, per state.build_state) confirmation state.
    """
    current = position
    updated = None
    closed = None

    for bar in new_bars:
        if current is None:
            break
        current, exit_event = apply_bar_to_open_position(current, bar, trail_pct)
        if exit_event is not None:
            closed = (current, exit_event)
            current = None
            updated = None  # mutually exclusive with `closed` -- a caller
            # applying tick results must never see both a still-open
            # "updated" position and a "closed" one for the same tick.
            break
        updated = current

    opened = None
    if current is None and new_bars and should_enter(
        was_confirmed_before=was_confirmed_before,
        is_confirmed_now=is_confirmed_now,
        position_open=False,
    ):
        entry_bar = new_bars[-1]
        entry_price = entry_bar["close"]
        opened = OpenPosition(
            id=None, symbol=symbol, entry_ts=entry_bar["ts"],
            entry_price=entry_price, high_water_mark=entry_price,
            stop_level=initial_stop_level(entry_price, trail_pct),
        )

    return JournalTick(opened=opened, updated=updated, closed=closed,
                       was_confirmed_after=is_confirmed_now)
