"""
Pure, framework-free indicator math. Every function here takes plain data in
and returns plain data out - no I/O, no threading, no Qt, no network. This is
deliberate: this is exactly the layer that got welded into a 2,288-line UI
controller in ToS_Companion, which made it impossible to trust or test in
isolation. It doesn't happen again here - this module has zero knowledge that
a UI, a broker, or a stream even exist.

A "bar" is a plain dict: {"ts": <unix seconds>, "open": float, "high": float,
"low": float, "close": float, "volume": float, "is_extended": bool}
"""
from __future__ import annotations
from dataclasses import dataclass


def session_vwap(bars: list[dict]) -> list[float]:
    """
    Cumulative session VWAP, resetting at the first bar in the list (caller
    is responsible for passing only bars from the current session - this
    function doesn't know what a "session boundary" is, on purpose).
    Returns one VWAP value per input bar.
    """
    out = []
    cum_pv = 0.0
    cum_vol = 0.0
    for b in bars:
        typical = (b["high"] + b["low"] + b["close"]) / 3.0
        cum_pv += typical * b["volume"]
        cum_vol += b["volume"]
        out.append(cum_pv / cum_vol if cum_vol > 0 else b["close"])
    return out


def ema(values: list[float], period: int) -> list[float]:
    """Standard exponential moving average. First value seeds on itself."""
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    """Returns {'macd': [...], 'signal': [...], 'histogram': [...]}, one value
    per input close, using EMA seeded on the first value (matches common
    charting-platform behavior closely enough for signal purposes; not meant
    to bit-match any specific vendor's warmup convention)."""
    fast_ema = ema(closes, fast)
    slow_ema = ema(closes, slow)
    macd_line = [f - s for f, s in zip(fast_ema, slow_ema)]
    signal_line = ema(macd_line, signal)
    histogram = [m - s for m, s in zip(macd_line, signal_line)]
    return {"macd": macd_line, "signal": signal_line, "histogram": histogram}


def relative_volume(bars: list[dict], lookback: int = 20) -> list[float]:
    """
    Each bar's volume divided by the rolling average of the preceding
    `lookback` bars. First `lookback` bars return 1.0 (not enough history
    to judge). This is deliberately a same-timeframe rolling comparison,
    not a comparison to a stale daily aggregate or to "yesterday" - see the
    conversation notes on why raw day-over-day volume comparison misleads
    given intraday volume's natural U-shape.
    """
    out = []
    for i, b in enumerate(bars):
        if i < lookback:
            out.append(1.0)
            continue
        window = bars[i - lookback:i]
        avg = sum(w["volume"] for w in window) / lookback
        out.append(b["volume"] / avg if avg > 0 else 1.0)
    return out


def continuation_days(daily_bars: list[dict], *, lookback_days: int,
                      threshold_pct: float) -> list[dict]:
    """Days, within the most recent `lookback_days` of `daily_bars`, whose
    day-over-day % change (close vs. prior close, SIGNED -- a big move
    either direction, not just up) exceeds `threshold_pct` in magnitude --
    {"ts", "pct_change"} dicts, oldest first. `threshold_pct` is a
    FRACTION (0.5 = 50%), the same convention every other "_pct" strategy
    parameter in this project uses.

    A day needs a PRIOR close to compute a % change from, so the window
    actually spans `lookback_days` + 1 bars of raw daily history; fewer
    bars than that (a symbol too new to have full history, or `daily_bars`
    empty/missing entirely) simply yields fewer comparisons, never a
    crash -- this function has no concept of "data unavailable" on its
    own, that distinction belongs to whoever calls it with real vs. empty
    input (specs.md section 7's continuation-vs-fresh-day gap: "unknown,
    no data" must never be confused with "checked, and it's fresh")."""
    if len(daily_bars) < 2:
        return []
    recent = daily_bars[-(lookback_days + 1):]
    out = []
    for i in range(1, len(recent)):
        prev_close = recent[i - 1]["close"]
        if prev_close == 0:
            continue
        pct_change = (recent[i]["close"] - prev_close) / prev_close
        if abs(pct_change) > threshold_pct:
            out.append({"ts": recent[i]["ts"], "pct_change": pct_change})
    return out
