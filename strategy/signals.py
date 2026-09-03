"""
Entry/exit signal logic.

Entry (two conditions must BOTH hold — this is what raises win rate honestly,
by stacking a trend filter on top of the pullback trigger, rather than by
picking a favorable backtest window):
  1. Trend filter: close > 200-day SMA  (only trade names in an established uptrend)
  2. Pullback trigger: RSI(14) < 30  OR  close <= 20-day SMA
     (price is temporarily oversold within that uptrend)

Exit (this is where expectancy is actually built):
  - Stop-loss:   entry_price - stop_atr_mult   * ATR   (volatility-adaptive, not a fixed %)
  - Target:      entry_price + target_atr_mult * ATR   (let winners run further than the stop risks)
  - Time exit:   close the trade after max_hold_days if neither hit (dead trades tie up capital)
"""
import pandas as pd

from .indicators import sma, rsi, atr
from config import StrategyConfig


def compute_indicators(df: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    """df must have columns: open, high, low, close, volume — indexed by date."""
    out = df.copy()
    out["sma_trend"] = sma(out["close"], cfg.trend_window)
    out["sma_pullback"] = sma(out["close"], cfg.pullback_window)
    out["rsi"] = rsi(out["close"], cfg.rsi_window)
    out["atr"] = atr(out["high"], out["low"], out["close"], cfg.atr_window)
    return out


def entry_signal(row: pd.Series, cfg: StrategyConfig) -> bool:
    if pd.isna(row["sma_trend"]) or pd.isna(row["atr"]) or pd.isna(row["sma_pullback"]):
        return False
    in_uptrend = row["close"] > row["sma_trend"]
    pulled_back = (row["rsi"] < cfg.rsi_oversold) or (row["close"] <= row["sma_pullback"])
    return bool(in_uptrend and pulled_back)


def stop_and_target(entry_price: float, atr_value: float, cfg: StrategyConfig):
    stop = entry_price - cfg.stop_atr_mult * atr_value
    target = entry_price + cfg.target_atr_mult * atr_value
    return stop, target
