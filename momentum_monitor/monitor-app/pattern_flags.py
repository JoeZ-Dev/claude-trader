"""
Flags a genuine bullish setup confirmation when enough factors OUTSIDE
the pattern itself disagree with it (specs.md section 37). Purely
observational: NEVER gates or affects should_enter/advance_journal or
any entry/exit decision -- entries/exits already fired (or didn't)
before this module is ever consulted, exactly the same "downstream of
the real decision" position narration.py already occupies. No I/O, no
SQLite, no network, mirroring this project's established pure-logic
boundary (core/indicators.py, journal_logic.py).

Bullish setup types only (resistance_breakout, micro_breakout,
vwap_reclaim, round_number_reclaim) -- breakdown types never reach this
module at all, by construction: they're not part of the `setups`/
`confirmed_types_after` tracking advance_journal already scopes to
bullish types only (see monitor-app/app.py's _maybe_flag_pattern, which
reuses that SAME confirmed-types diff, not a second detector).

The six flags, and a real naming decision worth recording: five of the
six ("macd_negative", "below_vwap", "below_day_open", "ema_misaligned",
"no_news") are named for the DISAGREEING condition itself -- TRUE always
means "this factor argues against the bullish confirmation." The
original design discussion's shorthand for the sixth was
"volume_confirmed" ("did real volume actually back this breakout") --
but stored literally under that name, TRUE would mean AGREEMENT, the
opposite polarity of the other five, which would silently corrupt
flag_count (summing a mix of "this disagrees" and "this agrees" as if
they meant the same thing). Named "volume_not_confirmed" here instead,
preserving the SAME real check (does real volume actually back this
move) but storing it with the SAME "TRUE = disagreement" polarity as
every other flag, so flag_count is a genuine, consistent count of
disagreeing factors, not an accidentally-mixed one.

`no_news` is explicitly the WEAKEST of the six: an empty/missing
watch_note means NOTHING WAS TYPED, never a confirmed absence of a real
catalyst (a real one could easily exist and just never have been
written down) -- best-effort only, labeled as such in FLAG_LABELS below
and wherever this is surfaced.
"""
from __future__ import annotations

from journal_logic import volume_gate_clears

# How many of the six flags must be TRUE (specs.md section 37) before a
# genuine confirmation gets recorded + narrated -- live-tunable
# (monitor-app/journal_store.py's strategy_params), this is only the
# starting default. 2 matches the design discussion's own reasoning: a
# SINGLE disagreeing factor is common and often noise (e.g. a genuinely
# strong breakout can still show macd_negative if momentum is just
# turning), but a COMBINATION of two or more independent factors all
# pointing the same (bearish) direction is a real, worth-narrating
# tension -- "a combination is usually the tell," not any one signal
# alone. Deliberately easy to raise without a redeploy if 2 turns out to
# be too noisy in practice against real volume -- the explicit plan is
# to watch real data and retune, not to have picked a permanently-right
# number up front.
DEFAULT_FLAG_COUNT_THRESHOLD = 2

# Plain-language label for each flag, reused both for narrative-prompt
# construction below AND anywhere this gets displayed -- one real string
# per flag, never re-worded ad hoc at each call site. no_news's own
# label carries its weaker-signal caveat inline, so it can never be
# shown without it.
FLAG_LABELS: dict[str, str] = {
    "volume_not_confirmed": "volume did not confirm the move",
    "macd_negative": "MACD is currently negative",
    "below_vwap": "price is below session VWAP",
    "below_day_open": "price is below today's opening price",
    "ema_misaligned": "EMA9 is below EMA20",
    "no_news": ("no watch note/catalyst is on record (best-effort only -- "
               "an empty note means nothing was typed, not a confirmed "
               "absence of a real catalyst)"),
}

# The six flag keys, in the fixed order flag_count sums over and
# pattern_flag_prompt lists them in -- one place, reused everywhere
# below, not re-typed per call site.
FLAG_KEYS: tuple[str, ...] = tuple(FLAG_LABELS.keys())


def compute_flags(
    *, price: float, macd: float, vwap: float | None, day_open: float | None,
    ema9: float, ema20: float, watch_note: str | None,
    relative_volume: float, volume_confirm_threshold: float,
    session_cumulative_volume: float = 0.0, avg_daily_volume: float | None = None,
    session_volume_multiple: float = 3.0,
) -> dict:
    """Computes all six flags (each independently TRUE/FALSE, never
    folded into one opaque score -- specs.md section 3's scoring-
    visibility principle, applied here too) plus their sum, flag_count.
    `vwap`/`day_open` genuinely CAN be None (specs.md's own state.py) --
    never crashes, never treats missing data as a disagreement."""
    flags = {
        "volume_not_confirmed": not volume_gate_clears(
            relative_volume, volume_confirm_threshold, session_cumulative_volume,
            avg_daily_volume, session_volume_multiple),
        "macd_negative": macd < 0,
        "below_vwap": vwap is not None and price < vwap,
        "below_day_open": day_open is not None and price < day_open,
        "ema_misaligned": ema9 < ema20,
        "no_news": not watch_note,
    }
    flags["flag_count"] = sum(1 for k in FLAG_KEYS if flags[k])
    return flags


def meets_flag_threshold(flag_count: int, *, threshold: int) -> bool:
    """True at or above threshold, same ">= " convention should_enter's
    own volume gate uses -- exactly at the threshold counts."""
    return flag_count >= threshold


def pattern_flag_prompt(symbol: str, setup_type: str, trigger_price: float,
                        distance: float, flags: dict) -> str:
    """Real numbers, real context, no fabrication -- same prompt-
    construction discipline as narration.py's confirmation_prompt/
    entry_prompt/exit_prompt. Lists ONLY the flags that are actually
    TRUE for this confirmation, using FLAG_LABELS' exact wording, never
    a summarized/paraphrased version that could drift from what was
    really computed."""
    active = [FLAG_LABELS[k] for k in FLAG_KEYS if flags.get(k)]
    factors_text = "; ".join(active)
    setup_label = setup_type.replace("_", " ")
    return (
        f"{symbol}: the {setup_label} setup just confirmed (trigger "
        f"{trigger_price}, {distance} away), but {flags['flag_count']} "
        f"external factor(s) disagree with it: {factors_text}. In two or "
        f"three plain-language sentences, explain what this tension means "
        f"for a trader watching this symbol -- a real, confirmed pattern "
        f"working against some headwinds, not a clean, uncontested setup."
    )
