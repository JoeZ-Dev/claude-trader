"""
Pluggable OHLCV data loading + point-in-time index membership.

- load_pit_membership(): reconstructs S&P 500 constituents as they stood on
  each historical date, from a vendored copy of the fja05680/sp500 dataset.
- load_yfinance(): real historical OHLCV. Requires network access to Yahoo
  Finance. Caches each pull on disk so repeated backtests over the same
  symbols/date range are reproducible and don't re-download.
- load_synthetic(): fake-but-plausible daily OHLCV so the strategy/backtest
  code can be exercised without network access. NEVER treat synthetic results
  as evidence the strategy works on real markets.
"""
from typing import Dict, List, Optional
import bisect
import hashlib
import os

import numpy as np
import pandas as pd


# --------------------------------------------------------------------------
# Point-in-time index membership
# --------------------------------------------------------------------------
class PITMembership:
    """
    Point-in-time membership lookup built from a CSV of (date, tickers) rows,
    where each row is the full constituent list effective on that date and the
    list only changes on rows where an add/drop actually happened.
    """

    def __init__(self, change_dates: List[pd.Timestamp], member_sets: List[frozenset]):
        self._dates = change_dates          # sorted ascending
        self._sets = member_sets            # aligned with _dates
        self._cache: Dict[pd.Timestamp, frozenset] = {}

    def members_asof(self, ts: pd.Timestamp) -> frozenset:
        """Constituents effective at `ts` (the most recent change on or before ts)."""
        ts = pd.Timestamp(ts)
        hit = self._cache.get(ts)
        if hit is not None:
            return hit
        i = bisect.bisect_right(self._dates, ts) - 1
        out = frozenset() if i < 0 else self._sets[i]
        self._cache[ts] = out
        return out

    def union(self, start, end) -> List[str]:
        """Every ticker that was a member on any date in [start, end]."""
        start, end = pd.Timestamp(start), pd.Timestamp(end)
        out: set = set()
        # membership effective at `start` (carried over from an earlier change)
        out |= set(self.members_asof(start))
        for d, s in zip(self._dates, self._sets):
            if d < start:
                continue
            if d > end:
                break
            out |= set(s)
        return sorted(out)


def load_pit_membership(csv_path: str) -> PITMembership:
    df = pd.read_csv(csv_path)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")
    dates = list(df["date"])
    sets = [frozenset(str(t).split(",")) for t in df["tickers"]]
    return PITMembership(dates, sets)


# --------------------------------------------------------------------------
# yfinance loader (real data) with on-disk cache
# --------------------------------------------------------------------------
def _yf_symbol(sym: str) -> str:
    """Map canonical index tickers to Yahoo's convention (class shares use '-')."""
    return sym.replace(".", "-")


def load_yfinance(
    symbols: List[str],
    start: str,
    end: str,
    min_rows: int = 250,
    cache_dir: str = "data/_cache",
) -> Dict[str, pd.DataFrame]:
    key_src = f"{sorted(symbols)}|{start}|{end}".encode()
    cache_path = os.path.join(cache_dir, hashlib.md5(key_src).hexdigest() + ".pkl")
    if os.path.exists(cache_path):
        print(f"[cache] loading yfinance data from {cache_path}")
        return pd.read_pickle(cache_path)

    try:
        import yfinance as yf
    except ImportError as e:
        raise ImportError("yfinance is not installed. Run: pip install yfinance") from e

    yf_map = {_yf_symbol(s): s for s in symbols}
    raw = yf.download(
        list(yf_map.keys()),
        start=start,
        end=end,
        group_by="ticker",
        auto_adjust=True,
        progress=False,
        threads=True,
    )

    data: Dict[str, pd.DataFrame] = {}
    thin = 0
    for yf_sym, canon in yf_map.items():
        try:
            df = raw[yf_sym].rename(columns=str.lower)
            df = df[["open", "high", "low", "close", "volume"]].dropna()
        except KeyError:
            thin += 1
            continue
        if len(df) < min_rows:
            thin += 1
            continue
        data[canon] = df.sort_index()

    print(f"[yfinance] {len(data)}/{len(symbols)} tickers returned >= {min_rows} rows "
          f"({thin} missing or too thin)")

    os.makedirs(cache_dir, exist_ok=True)
    pd.to_pickle(data, cache_path)
    return data


# --------------------------------------------------------------------------
# Synthetic loader (no network; code-exercise only)
# --------------------------------------------------------------------------
def load_synthetic(
    symbols: List[str],
    start: str,
    end: str,
    seed: int = 42,
    annual_drift: float = 0.08,
    annual_vol: float = 0.25,
) -> Dict[str, pd.DataFrame]:
    """
    Geometric-Brownian-motion-ish daily bars with a slight upward drift, so the
    trend filter has something to latch onto. Only for exercising the code,
    not for evaluating the strategy's real edge.
    """
    dates = pd.bdate_range(start=start, end=end)
    n = len(dates)
    daily_drift = annual_drift / 252
    daily_vol = annual_vol / np.sqrt(252)

    data = {}
    for i, sym in enumerate(symbols):
        r = np.random.default_rng(seed + i)
        returns = r.normal(daily_drift, daily_vol, n)
        close = 100 * np.cumprod(1 + returns)

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
