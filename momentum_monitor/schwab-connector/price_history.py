"""
Current-day historical-bar backfill for schwab-connector.

Fixes a real gap found via live comparison against a real chart (RUNBOOK.md
DoD check 4, run against QCLS): a symbol newly watched mid-session had its
VWAP/EMA/MACD/level-detection compute only over bars captured SINCE
POST /watch was called, not over the actual trading session -- producing a
meaningfully wrong VWAP whenever a symbol is added after a big move is
already underway. That's this tool's normal use case (a candidate gets
added mid-session because something is happening), not an edge case, so a
cold start is the wrong default.

The fix: before Connector._consume (app.py) starts draining live ticks into
the BarStore, it backfills today's session bars via this module and appends
them first -- so the stored series, and therefore session_vwap/ema/macd/
detect_levels in momentum_monitor/core, actually start from market open (or
the first extended-hours bar, per specs.md/RUNBOOK.md's VWAP-anchor
decision), not from whenever the symbol happened to get watched.

candles_to_bars() is the pure, tested payload -> bar mapping (per
AGENT_PROTOCOL.md: no test touches the live Schwab API). fetch_today_bars()
is the thin, network-calling seam around it, mirroring how stream.py keeps
message_to_ticks() pure and SchwabStreamSource thin.

Granularity: Schwab's price-history endpoint's finest resolution is
1-minute candles (confirmed by reading schwab-py's PriceHistory.Frequency
enum in client/base.py -- there is no sub-minute frequency; EVERY_MINUTE is
the smallest). So backfilled bars are coarser than the 10s bars the live
aggregator produces from streaming ticks. That's fine: session_vwap / ema /
macd / detect_levels (momentum_monitor/core) take a plain bar list and
don't assume uniform spacing, so a series that's 1-minute-granularity early
and 10s-granularity later is a correct input, not a bug.

periodType=day/period=1 (via get_price_history directly, NOT the
get_price_history_every_minute convenience wrapper, which always sends a
broad default date range alongside period=1 and documents itself as
returning "up to 48 days of data" as a result) is what actually constrains
the request to the current trading day, matching what a chart's default
"1 Day" view shows.
"""
from __future__ import annotations

from aggregator import is_extended_hours


def candles_to_bars(candles: list[dict]) -> list[dict]:
    """Map Schwab's price-history candle shape
    ({"datetime": <epoch ms>, "open", "high", "low", "close", "volume"}) to
    the bar contract in specs.md section 4. A candle with no usable
    `datetime` is dropped. Output is sorted by ts (Schwab returns candles
    in order already; sorting is cheap insurance, not a correction of any
    known misbehavior)."""
    out = []
    for c in candles:
        dt = c.get("datetime")
        if dt is None:
            continue
        ts = int(dt) // 1000
        out.append({
            "ts": ts,
            "open": float(c["open"]),
            "high": float(c["high"]),
            "low": float(c["low"]),
            "close": float(c["close"]),
            "volume": float(c.get("volume", 0.0)),
            "is_extended": is_extended_hours(ts),
        })
    out.sort(key=lambda b: b["ts"])
    return out


async def fetch_today_bars(client, symbol: str) -> list[dict]:
    """Fetch the current trading day's bars at 1-minute granularity
    (Schwab's finest available resolution), including extended hours, and
    return them in the bar contract shape, oldest first.

    Raises on any non-2xx response or network failure. Callers decide
    whether that's fatal -- Connector._consume (app.py) treats a backfill
    failure as non-fatal: live streaming still starts, so a transient
    price-history error doesn't block the whole pipeline the way a
    permanently-cold-started VWAP silently would."""
    resp = await client.get_price_history(
        symbol,
        period_type=client.PriceHistory.PeriodType.DAY,
        period=client.PriceHistory.Period.ONE_DAY,
        frequency_type=client.PriceHistory.FrequencyType.MINUTE,
        frequency=client.PriceHistory.Frequency.EVERY_MINUTE,
        need_extended_hours_data=True,
    )
    resp.raise_for_status()
    payload = resp.json()
    return candles_to_bars(payload.get("candles", []))
