"""
narration.py -- event-triggered narration logic for phase 3 stage 1
(specs.md section 27): pure functions for detecting the three trigger
events, composing their prompts, and evaluating the two safety gates.
No I/O here -- Poller (app.py) is the only place that actually calls
claude-connector or holds narration state, mirroring this project's
"core is pure, the app layer does I/O" separation (specs.md section 3).

Scope, explicitly (specs.md section 27): heavy-tier, event-triggered
narration ONLY, on exactly three trigger events -- reusing detection
that already exists elsewhere, never inventing new "was this
meaningful" logic:
  1. A setup's hold_confirmed transitioning False->True, for one of the
     four entry-eligible bullish setup types (the same types tracked by
     journal_logic.py's own was_confirmed_types/confirmed_types_after
     bookkeeping). Breakdown-below types have no such transition
     tracking anywhere in this codebase (specs.md section 22: they're
     structurally separate from `setups`/advance_journal entirely) and
     are deliberately out of scope here rather than inventing new
     tracking for them.
  2. A real entry firing (advance_journal's tick.opened).
  3. A real exit firing, with its P&L (advance_journal's tick.closed).
Ongoing lighter-tier updates are explicitly deferred to a later stage
(specs.md section 27's own "explicit decision to defer" note).
"""
from __future__ import annotations


# -- trigger 1 -----------------------------------------------------------

def newly_confirmed_types(confirmed_types_after: frozenset[str],
                          was_confirmed_types: frozenset[str]) -> frozenset[str]:
    """A pure set difference over journal_logic.advance_journal's own
    already-computed confirmed_types_after/was_confirmed_types -- the
    SAME bookkeeping should_enter's own freshly-confirmed check already
    relies on, no new "was this meaningful" logic invented. Deliberately
    does NOT re-derive freshness/volume gating (that governs ENTRY, a
    different question from "is this worth narrating") -- every type
    that newly confirmed this tick is narration-worthy, whether or not
    it also happened to clear the separate gates required to fire an
    entry."""
    return confirmed_types_after - was_confirmed_types


# -- prompts: simple, factual, additive commentary -- not a second ------
# detection system, just a plain-language description of something the
# system has already mechanically decided and logged.

def confirmation_prompt(symbol: str, setup_type: str, setup: dict) -> str:
    return (
        f"{symbol}: the {setup_type.replace('_', ' ')} setup just confirmed "
        f"(price held above the trigger level for the required time). "
        f"Trigger price {setup['trigger_price']}, currently {setup['distance']} "
        f"away. In one or two plain-language sentences, note what this means "
        f"for a trader watching this symbol."
    )


def entry_prompt(symbol: str, position) -> str:
    shares_text = (f"{position.shares} shares" if position.shares is not None
                   else "size not computed")
    setup_type = (position.setup_type or "unknown").replace("_", " ")
    return (
        f"{symbol}: a virtual long position just opened on the {setup_type} "
        f"setup at {position.entry_price} ({shares_text}). In one or two "
        f"plain-language sentences, note this entry."
    )


def exit_prompt(symbol: str, position, exit_event, pnl_pct: float | None,
                pnl_dollars: float | None) -> str:
    pnl_text = f"{pnl_pct:+.2f}%" if pnl_pct is not None else "P&L not available"
    dollars_text = f" (${pnl_dollars:+.2f})" if pnl_dollars is not None else ""
    exit_reason = exit_event.exit_reason.replace("_", " ")
    return (
        f"{symbol}: the virtual position from {position.entry_price} just "
        f"closed at {exit_event.exit_price} via {exit_reason}, "
        f"{pnl_text}{dollars_text}. In one or two plain-language sentences, "
        f"summarize this outcome."
    )


# -- safety gate 2: mandatory hourly re-arm ------------------------------

def is_armed(armed_until: float | None, now: float) -> bool:
    """True only while strictly before the last explicit arm/re-arm
    action's expiry. `armed_until is None` (never armed, or a fresh
    restart -- specs.md section 27: both gates default to disarmed on
    every restart) is always False, never treated as "armed forever."""
    return armed_until is not None and now < armed_until


# -- safety gate 1: rate-limit circuit breaker ---------------------------

def prune_and_record_call(call_timestamps: list[float], now: float,
                          window_minutes: float) -> list[float]:
    """Returns a NEW list: entries older than the rolling window dropped,
    `now` appended -- pure, the caller (Poller) owns persisting the
    result onto its own in-memory state."""
    cutoff = now - window_minutes * 60.0
    return [t for t in call_timestamps if t >= cutoff] + [now]


def breaker_should_trip(call_timestamps: list[float], max_calls_per_window: float) -> bool:
    """True once STRICTLY MORE than max_calls_per_window calls have
    landed within the current window (specs.md section 27: "more than
    MAX_CALLS_PER_WINDOW calls ... trip the breaker") -- exactly at the
    threshold does not trip; the next call after that does."""
    return len(call_timestamps) > max_calls_per_window
