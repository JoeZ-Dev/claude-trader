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
    # --- Backtest universe: point-in-time S&P 500 membership ---
    # The backtest no longer uses a hand-picked list. Instead it reconstructs
    # S&P 500 membership as it actually stood on each trading day, so a name is
    # only eligible for entry while it was really in the index, and names that
    # were later removed (acquired, shrank out, went bankrupt) are included for
    # the period they were members. This removes the "we already know these
    # specific tickers won" selection bias.
    #
    # Source: https://github.com/fja05680/sp500  (MIT license), vendored
    # unmodified at the path below. See data/loader.py:load_pit_membership and
    # data/sp500_constituents.SOURCE.md.
    constituents_file: str = "data/sp500_constituents.csv"

    # Data-layer limitation, documented not silently ignored: free yfinance
    # history is unavailable for most tickers that left the index via merger or
    # bankruptcy, so a residual survivorship bias remains at the data level even
    # though the membership list itself is now point-in-time. run_backtest.py
    # prints how many requested tickers actually returned usable data.

    benchmark: str = "SPY"

    # --- Live paper-trader universe ONLY (not used by the backtest) ---
    # live/alpaca_paper_trader.py still imports `.symbols`; it is frozen this
    # round, so this small explicit list stays here to keep that script working
    # unchanged. The backtest ignores this field entirely.
    symbols: List[str] = field(default_factory=lambda: [
        "AAPL", "MSFT", "NVDA", "GOOGL", "META", "CRM", "ADBE",
        "AMZN", "COST", "HD", "MCD", "NKE",
        "UNH", "JNJ", "ABBV", "LLY",
        "JPM", "V", "MA", "GS",
        "CAT", "HON", "XOM", "CVX",
        "DIS", "PG", "KO", "PEP",
    ])
