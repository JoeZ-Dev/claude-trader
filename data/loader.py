"""
Pluggable OHLCV data loading.

- load_yfinance(): real historical data. Requires network access to Yahoo
  Finance, which is NOT available in this sandbox — run this locally.
- load_synthetic(): generates fake-but-plausible daily OHLCV data with a mild
  upward drift plus noise, purely so the strategy/backtest code can be
  exercised and validated without network access. NEVER treat synthetic
  results as evidence the strategy works on real markets.
"""
from typing import Dict, List
import numpy as np
import pandas as pd


def load_yfinance(symbols: List[str], start: str, end: str) -> Dict[str, pd.DataFrame]:
    try:
        import yfinance as yf
    except ImportError as e:
        raise ImportError(
            "yfinance is not installed. Run: pip install yfinance"
        ) from e

    data = {}
    raw = yf.download(symbols, start=start, end=end, group_by="ticker", auto_adjust=True)
    for sym in symbols:
        try:
            df = raw[sym].rename(columns=str.lower)
            df = df[["open", "high", "low", "close", "volume"]].dropna()
            data[sym] = df
        except KeyError:
            print(f"[warn] no data returned for {sym}, skipping")
    return data


def load_synthetic(
    symbols: List[str],
    start: str,
    end: str,
    seed: int = 42,
    annual_drift: float = 0.08,
    annual_vol: float = 0.25,
) -> Dict[str, pd.DataFrame]:
    """
    Generates geometric-Brownian-motion-ish daily bars with a slight upward
    drift, so the trend filter has something to latch onto — this is only
    for exercising the code, not for evaluating the strategy's real edge.
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start=start, end=end)
    n = len(dates)
    daily_drift = annual_drift / 252
    daily_vol = annual_vol / np.sqrt(252)

    data = {}
    for i, sym in enumerate(symbols):
        # Vary each symbol's seed so they aren't identical/perfectly correlated
        r = np.random.default_rng(seed + i)
        returns = r.normal(daily_drift, daily_vol, n)
        close = 100 * np.cumprod(1 + returns)

        # Fabricate a plausible OHLC around each day's close
        intraday_range = np.abs(r.normal(0, daily_vol * 0.6, n)) * close
        high = close + intraday_range * r.uniform(0.3, 1.0, n)
        low = close - intraday_range * r.uniform(0.3, 1.0, n)
        open_ = low + (high - low) * r.uniform(0.2, 0.8, n)
        volume = r.integers(1_000_000, 8_000_000, n)

        df = pd.DataFrame(
            {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
            index=dates,
        )
        data[sym] = df
    return data
