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

Date range (fetch_today_bars): an explicit start_datetime/end_datetime
pair is used, NOT period_type=DAY/period=ONE_DAY. That period-based form
was tried first and confirmed LIVE (backfilling QCLS on 2026-09-16,
against the real Schwab API, not a guess) to return the PREVIOUS
completed trading day when no date range is also given, not the current
in-progress session -- matching get_price_history's own docstring
("end_datetime: ... Default is previous trading day"). That silently
reproduced this exact cold-start VWAP bug one day later, since the live
bar was the only one left matching "today" once session_bars_for_vwap
(monitor-app/state.py) filtered by calendar date. An explicit range
(today's NY midnight through now) sidesteps Schwab's period-based
default entirely and is unambiguous about what "today" means.

Correction (specs.md section 33): the above is specific to
`fetch_today_bars`' MINUTE frequency, NOT a blanket "never pass
period_type" rule -- `fetch_daily_history` below genuinely NEEDS
`period_type=YEAR` alongside its own explicit dates, a real regression
found live (avg_daily_volume/continuation/market_backdrop all silently
degraded to "unknown" for weeks): Schwab's real API defaults
`periodType` to DAY whenever it's omitted, and DAY only accepts
`frequencyType=minute` -- which is why omitting `period_type` happened
to work for `fetch_today_bars` (MINUTE) but returns a genuine 400 for
`fetch_daily_history` (DAILY: "Invalid frequencyType DAILY for
periodType DAY"). Confirmed live, separately, that Schwab still honors
explicit start_datetime/end_datetime with `period_type` present -- only
`period` (the separate COUNT parameter) actually conflicts with
explicit dates, per get_price_history's own docstring. The original
"period_type/period must both be absent" conclusion conflated two
independent parameters that only happened to both be irrelevant for the
MINUTE case tested at the time.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aggregator import is_extended_hours

_NY = ZoneInfo("America/New_York")


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


async def fetch_today_bars(client, symbol: str, *, now_fn=time.time) -> list[dict]:
    """Fetch the current trading day's bars so far, at 1-minute granularity
    (Schwab's finest available resolution), including extended hours, and
    return them in the bar contract shape, oldest first.

    Requests an explicit [today's NY midnight, now] range rather than
    period_type=DAY/period=ONE_DAY -- see the module docstring for why the
    period-based form is a trap that silently returns yesterday.

    Raises on any non-2xx response or network failure. Callers decide
    whether that's fatal -- Connector._consume (app.py) treats a backfill
    failure as non-fatal: live streaming still starts, so a transient
    price-history error doesn't block the whole pipeline the way a
    permanently-cold-started VWAP silently would."""
    now = datetime.fromtimestamp(now_fn(), _NY)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    resp = await client.get_price_history(
        symbol,
        frequency_type=client.PriceHistory.FrequencyType.MINUTE,
        frequency=client.PriceHistory.Frequency.EVERY_MINUTE,
        start_datetime=start_of_day,
        end_datetime=now,
        need_extended_hours_data=True,
    )
    resp.raise_for_status()
    payload = resp.json()
    return candles_to_bars(payload.get("candles", []))


async def fetch_daily_history(client, symbol: str, *, lookback_days: int = 30,
                              now_fn=time.time, include_today: bool = False) -> list[dict]:
    """Fetch up to `lookback_days` of DAILY candles for the session-level
    volume gate's "typical daily volume" baseline (specs.md section 12) --
    a longer, coarser lookback distinct from fetch_today_bars' same-day
    intraday backfill above, used to average a symbol's normal daily
    volume against today's in-progress cumulative volume.

    Same explicit start_datetime/end_datetime discipline as
    fetch_today_bars, for the same reason (see that function's docstring
    on why period_type=DAY silently returns the wrong range) --
    end_datetime is today's own NY midnight, EXCLUSIVE of today by
    default, so a still-forming partial session never drags the average
    down. start_datetime requests a calendar window generously larger
    than `lookback_days` (weekends/holidays mean calendar days always
    outnumber trading days) -- 2x plus a 10-day pad is comfortable
    without over-fetching. Returns at most the `lookback_days` MOST
    RECENT candles actually returned, oldest-first (candles_to_bars'
    own sort order) -- a symbol with less than `lookback_days` of real
    trading history simply returns fewer, never padded or invented.

    `include_today` (specs.md section 23, market backdrop display) --
    when True, end_datetime is `now` instead of today's midnight, so the
    still-forming CURRENT day's daily candle is included too, with its
    `close` being Schwab's continuously-updating last-traded price for
    the session so far. This is the opposite need from every existing
    caller above (which deliberately excludes today to keep the average
    honest) -- built for a caller that wants exactly one thing: today's
    live price compared against the prior COMPLETED day's close. Default
    False preserves every existing caller's behavior byte-for-byte.

    Raises on any non-2xx response or network failure, same as
    fetch_today_bars -- callers decide whether that's fatal.

    `period_type=YEAR` is REQUIRED here (specs.md section 33) -- a real
    regression found live: Schwab's real API defaults `periodType` to
    DAY whenever it's omitted (confirmed directly, verbatim response:
    "Invalid frequencyType DAILY for periodType DAY"), and DAY only
    accepts `frequencyType=minute` -- exactly what `fetch_today_bars`
    above wants, which is WHY omitting `period_type` happened to work
    there, not because `period_type` itself is unsafe to pass alongside
    explicit dates. Confirmed live, separately, that Schwab still honors
    the EXPLICIT `start_datetime`/`end_datetime` given below with
    `period_type` present -- it does not fall back to some period-based
    range. `period` (the separate COUNT parameter) is the one that
    actually conflicts with explicit dates, per get_price_history's own
    docstring, and stays omitted. YEAR (not MONTH) is used regardless of
    `lookback_days`, since the explicit date range below already governs
    exactly what's fetched -- `period_type` here only has to be VALID
    for `frequencyType=DAILY`, not match the lookback precisely."""
    now = datetime.fromtimestamp(now_fn(), _NY)
    end = now if include_today else now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=lookback_days * 2 + 10)
    resp = await client.get_price_history(
        symbol,
        period_type=client.PriceHistory.PeriodType.YEAR,
        frequency_type=client.PriceHistory.FrequencyType.DAILY,
        frequency=client.PriceHistory.Frequency.DAILY,
        start_datetime=start,
        end_datetime=end,
        need_extended_hours_data=False,
    )
    resp.raise_for_status()
    payload = resp.json()
    bars = candles_to_bars(payload.get("candles", []))
    return bars[-lookback_days:]
