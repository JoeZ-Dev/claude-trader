"""
Loss monitoring & evaluation view -- retrospective, offline analysis of
the virtual trade journal's OWN accumulated history (specs.md section
26). Pure functions over a list of closed-trade dicts (the same shape
JournalStore.recent_closed() already returns) -- no I/O, no database
access of its own, mirroring this project's "core is pure, the app
layer does I/O" separation (specs.md section 3).

This replaces the deferred portfolio-risk-cap gap as the priority
(specs.md section 26): correctness and evaluation of the strategy logic
matter more than a loss limit in a paper-trading context where a "loss"
costs nothing real. The point is understanding whether the strategy is
trustworthy, with particular focus on losses -- not a generic dashboard.

Standing rule (specs.md section 6): a `symbol_switched` exit is
watchlist housekeeping, never a real trading outcome. `real_trades()`
below is the ONE place this filter is implemented; every function in
this module calls it first (directly or via another function here) and
none reimplements the check.

Honesty requirement (explicit user instruction, echoing this project's
own EOD-swing-bot small-sample lesson): a breakdown with too few trades
to be meaningful says so explicitly, in an actual "note" string included
in the output, rather than presenting a misleadingly confident
percentage or silently omitting the caveat. MIN_TRADES_FOR_STATS/
MIN_BUCKET_SIZE below are the two sample-size floors used throughout --
the former for a whole group (overall, one setup_type, one review_label,
the reviewed-trades slice as a whole), the latter for a single narrow
bucket within the losses section's clustering view (one symbol, one
hour-of-day), where requiring MIN_TRADES_FOR_STATS in every individual
bucket would report nothing at all against realistically thin early
data.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

_NY = ZoneInfo("America/New_York")

# The ONLY real trading-outcome exit reasons (specs.md section 6) --
# symbol_switched is watchlist housekeeping and must never be counted as
# strategy performance. Expand this set if a real target_hit exit reason
# is ever added (specs.md section 6 already anticipates this); symbol_
# switched must never join it.
REAL_TRADE_EXIT_REASONS = frozenset({"trailing_stop"})

# Below this many trades, a win-rate/expectancy figure is reported WITH
# an explicit "too few trades to be meaningful" note, never silently
# hidden and never presented bare as if it were confident -- this
# project already learned that lesson once, with the EOD swing bot's
# early small-sample results.
MIN_TRADES_FOR_STATS = 10

# A lower floor for a single narrow bucket (one symbol, one hour-of-day)
# within the losses-clustering view -- requiring MIN_TRADES_FOR_STATS in
# every individual bucket would mean that section reports nothing at all
# on realistically thin early data. Still flagged explicitly below this,
# same honesty treatment, just a more permissive threshold for a
# narrower question ("does THIS bucket look elevated") than "is this
# whole group's stat trustworthy."
MIN_BUCKET_SIZE = 3


def real_trades(closed: list[dict]) -> list[dict]:
    """Every closed-trade dict in `closed` whose exit_reason is a real
    trading outcome (specs.md section 6) -- symbol_switched rows
    excluded. The ONE place this filter happens; every function below
    calls this (directly or transitively) before computing anything."""
    return [t for t in closed if t.get("exit_reason") in REAL_TRADE_EXIT_REASONS]


def _sufficiency_note(n: int, floor: int = MIN_TRADES_FOR_STATS) -> str | None:
    if n == 0:
        return "no real trades yet -- nothing to report"
    if n < floor:
        plural = "s" if n != 1 else ""
        return (f"based on only {n} trade{plural} -- too few to be "
               f"statistically meaningful (want at least {floor})")
    return None


def _win_loss_stats(trades: list[dict]) -> dict:
    """wins/losses/breakeven counted by realized_pnl_pct sign. win_rate
    is wins / count (over ALL trades in the group, breakeven included in
    the denominator but not the numerator) -- a trade that closes
    exactly flat is neither a win nor a loss, and forcing it into either
    bucket would misstate both. expectancy_pct is the plain mean of
    realized_pnl_pct across the group."""
    n = len(trades)
    wins = sum(1 for t in trades if t["realized_pnl_pct"] > 0)
    losses = sum(1 for t in trades if t["realized_pnl_pct"] < 0)
    breakeven = n - wins - losses
    return {
        "count": n,
        "wins": wins,
        "losses": losses,
        "breakeven": breakeven,
        "win_rate": (wins / n) if n else None,
        "expectancy_pct": (sum(t["realized_pnl_pct"] for t in trades) / n) if n else None,
        "sufficient_sample": n >= MIN_TRADES_FOR_STATS,
        "note": _sufficiency_note(n),
    }


def overall_stats(closed: list[dict]) -> dict:
    """Win rate and expectancy across every real (specs.md section 6)
    closed trade, regardless of setup_type or review status."""
    return _win_loss_stats(real_trades(closed))


def breakdown_by_setup_type(closed: list[dict]) -> dict[str, dict]:
    """Win rate/expectancy grouped by setup_type, among real trades only
    -- answers "which of the four entry-eligible types produces the best/
    worst real results" (specs.md section 26). A trade with no setup_type
    at all (a pre-migration row) groups under the explicit "unknown" key,
    never silently dropped."""
    groups: dict[str, list[dict]] = {}
    for t in real_trades(closed):
        groups.setdefault(t.get("setup_type") or "unknown", []).append(t)
    return {key: _win_loss_stats(group) for key, group in groups.items()}


def breakdown_by_review_label(closed: list[dict]) -> dict:
    """Win rate/expectancy grouped by review_label, among real trades
    that HAVE been reviewed -- the actual point of the feature (specs.md
    section 26): does `bad_signal` correlate with losses (validating the
    label means something), and do `clean_signal` trades still lose
    sometimes (expected and healthy -- not every good signal wins). An
    unreviewed trade (review_label is None) has no label to group under
    and is excluded from `breakdown`, but `reviewed_count`/
    `total_real_count` are both reported explicitly so the reader can
    see how much of the real history has been reviewed at all, not just
    how the reviewed slice breaks down. `note` reflects the sufficiency
    of the REVIEWED slice as a whole (not any one label's own count) --
    "once enough reviewed trades exist to be meaningful," per the
    feature's own framing."""
    trades = real_trades(closed)
    reviewed = [t for t in trades if t.get("review_label")]
    groups: dict[str, list[dict]] = {}
    for t in reviewed:
        groups.setdefault(t["review_label"], []).append(t)
    return {
        "total_real_count": len(trades),
        "reviewed_count": len(reviewed),
        "breakdown": {key: _win_loss_stats(group) for key, group in groups.items()},
        "note": _sufficiency_note(len(reviewed)),
    }


def _entry_hour(t: dict) -> int:
    return datetime.fromtimestamp(t["entry_ts"], _NY).hour


def _loss_cluster_by(trades: list[dict], losses: list[dict], *, key_fn,
                     overall_rate: float | None) -> dict:
    """Per-bucket loss RATE (losses in bucket / trades in bucket),
    compared against `overall_rate` -- "clusters" here means "loses more
    often than the overall rate," not just "has more raw losses than
    other buckets," which would just reflect trade-count share and say
    nothing about actual risk concentration. Each bucket's own sample
    size is honestly flagged separately (MIN_BUCKET_SIZE, a lower floor
    than MIN_TRADES_FOR_STATS -- see module docstring): a bucket below it
    is never flagged `elevated_vs_overall`, no matter its raw loss rate,
    since a single unlucky trade would otherwise misrepresent a coin
    flip as a pattern."""
    trade_buckets: dict = {}
    for t in trades:
        trade_buckets.setdefault(key_fn(t), []).append(t)
    loss_buckets: dict = {}
    for t in losses:
        loss_buckets.setdefault(key_fn(t), []).append(t)

    out = {}
    for key, bucket_trades in trade_buckets.items():
        bucket_losses = loss_buckets.get(key, [])
        n = len(bucket_trades)
        loss_rate = (len(bucket_losses) / n) if n else None
        sufficient = n >= MIN_BUCKET_SIZE
        elevated = (sufficient and overall_rate is not None
                   and loss_rate is not None and loss_rate > overall_rate)
        out[key] = {
            "trades": n,
            "losses": len(bucket_losses),
            "loss_rate": loss_rate,
            "sufficient_sample": sufficient,
            "elevated_vs_overall": elevated,
        }
    return out


def losses_section(closed: list[dict]) -> dict:
    """Dedicated view of losing real trades (specs.md section 26) --
    average loss size, and whether losses cluster by setup_type,
    review_label, symbol, or entry hour-of-day (America/New_York, same
    exchange-local timezone convention this project already anchors
    session VWAP to). Answers "when we lose, is it expected noise around
    a sound process, or a sign something's actually wrong" -- not a
    generic loss list."""
    trades = real_trades(closed)
    losses = [t for t in trades if t["realized_pnl_pct"] < 0]
    n = len(trades)
    n_losses = len(losses)
    overall_loss_rate = (n_losses / n) if n else None

    dollar_losses = [t["realized_pnl_dollars"] for t in losses
                     if t.get("realized_pnl_dollars") is not None]

    reviewed_trades = [t for t in trades if t.get("review_label")]
    reviewed_losses = [t for t in losses if t.get("review_label")]

    return {
        "count": n_losses,
        "total_real_count": n,
        "avg_loss_pct": (sum(t["realized_pnl_pct"] for t in losses) / n_losses)
                        if n_losses else None,
        "avg_loss_dollars": (sum(dollar_losses) / len(dollar_losses))
                            if dollar_losses else None,
        "overall_loss_rate": overall_loss_rate,
        "note": _sufficiency_note(n),
        "by_setup_type": _loss_cluster_by(
            trades, losses, key_fn=lambda t: t.get("setup_type") or "unknown",
            overall_rate=overall_loss_rate),
        "by_review_label": _loss_cluster_by(
            reviewed_trades, reviewed_losses, key_fn=lambda t: t["review_label"],
            overall_rate=overall_loss_rate),
        "by_symbol": _loss_cluster_by(
            trades, losses, key_fn=lambda t: t["symbol"], overall_rate=overall_loss_rate),
        "by_hour_of_day": _loss_cluster_by(
            trades, losses, key_fn=_entry_hour, overall_rate=overall_loss_rate),
    }
