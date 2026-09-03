"""
Central configuration for the trend-filtered mean-reversion strategy.

Design goal: EXPECTANCY, not win rate. See README.md for the reasoning.
"""
from dataclasses import dataclass, field
from typing import List


@dataclass
class StrategyConfig:
    # --- Trend filter ---
    trend_window: int = 200          # only trade names above this SMA (uptrend context)

    # --- Entry (pullback) ---
    pullback_window: int = 20        # short MA used as a pullback reference
    rsi_window: int = 14
    rsi_oversold: float = 30.0       # RSI below this = oversold trigger

    # --- Volatility / risk ---
    atr_window: int = 14
    stop_atr_mult: float = 2.0       # stop = entry - 2x ATR
    target_atr_mult: float = 4.0     # target = entry + 4x ATR  (2:1 reward:risk baseline)
    max_hold_days: int = 20          # time-based exit if neither stop nor target hit

    # --- Position sizing / portfolio risk ---
    risk_pct_per_trade: float = 0.01     # risk 1% of equity per trade
    max_open_positions: int = 6
    max_portfolio_heat: float = 0.06     # sum of open-trade risk capped at 6% of equity

    # --- Costs (keep the backtest honest) ---
    commission_per_share: float = 0.0    # Alpaca is commission-free; set >0 to be conservative
    slippage_bps: float = 5.0            # 5 basis points assumed slippage on entries/exits

    # --- Starting capital ---
    starting_equity: float = 100_000.0


@dataclass
class UniverseConfig:
    # ~28 liquid, well-known large caps across sectors, so a first backtest
    # tests generalization rather than fitting to one or two names.
    symbols: List[str] = field(default_factory=lambda: [
        # Tech
        "AAPL", "MSFT", "NVDA", "GOOGL", "META", "CRM", "ADBE",
        # Consumer
        "AMZN", "COST", "HD", "MCD", "NKE",
        # Healthcare
        "UNH", "JNJ", "ABBV", "LLY",
        # Financials
        "JPM", "V", "MA", "GS",
        # Industrials / Energy
        "CAT", "HON", "XOM", "CVX",
        # Comms / Staples
        "DIS", "PG", "KO", "PEP",
    ])
    benchmark: str = "SPY"
