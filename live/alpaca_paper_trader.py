"""
Live PAPER trading executor using Alpaca.

IMPORTANT — read before running:
  - This connects to Alpaca's PAPER trading endpoint only (paper-api.alpaca.markets).
    Double-check APCA_API_BASE_URL below before ever pointing this at a live account.
  - Run this on your own machine or your own scheduled job (e.g. a Claude Code
    scheduled run, a cron job, a small always-on VM). Do NOT paste your API
    keys into a chat window — set them as environment variables or in a
    local, gitignored .env file instead.
  - This script applies the SAME entry/exit logic as the backtest
    (strategy/signals.py) so live behavior matches what was tested. It does
    not re-implement the strategy — it imports it.

Setup:
  1. Create a free Alpaca account -> generate PAPER API keys (dashboard has
     a "Paper Trading" toggle).
  2. export APCA_API_KEY_ID=your_key_id
     export APCA_API_SECRET_KEY=your_secret_key
  3. pip install alpaca-py
  4. python live/alpaca_paper_trader.py   (intended to run once per trading day,
     e.g. via a scheduled task shortly after market open)
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import StrategyConfig, UniverseConfig
from strategy.signals import compute_indicators, entry_signal, stop_and_target

PAPER_BASE_URL = "https://paper-api.alpaca.markets"  # NEVER change to the live endpoint here


def get_clients():
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.data.historical import StockHistoricalDataClient
    except ImportError as e:
        raise ImportError("Run: pip install alpaca-py") from e

    key = os.environ.get("APCA_API_KEY_ID")
    secret = os.environ.get("APCA_API_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError(
            "Set APCA_API_KEY_ID and APCA_API_SECRET_KEY environment variables "
            "(use your PAPER keys, not live keys)."
        )

    trading_client = TradingClient(key, secret, paper=True)  # paper=True is the safety switch
    data_client = StockHistoricalDataClient(key, secret)
    return trading_client, data_client


def fetch_recent_bars(data_client, symbol: str, lookback_days: int = 260):
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    import pandas as pd
    from datetime import datetime, timedelta

    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=datetime.utcnow() - timedelta(days=int(lookback_days * 1.6)),  # padding for weekends/holidays
    )
    bars = data_client.get_stock_bars(req).df
    if bars.empty:
        return None
    df = bars.reset_index(level=0, drop=True) if "symbol" in bars.index.names else bars
    df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
    return df


def run_once():
    """Check signals across the universe and place/manage paper orders. Meant to run once/day."""
    from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce

    cfg = StrategyConfig()
    universe = UniverseConfig()
    trading_client, data_client = get_clients()

    account = trading_client.get_account()
    equity = float(account.equity)
    positions = {p.symbol: p for p in trading_client.get_all_positions()}

    print(f"[paper] equity=${equity:,.2f} open_positions={len(positions)}")

    for symbol in universe.symbols:
        if symbol in positions:
            continue  # position management/exits best handled via bracket orders, see note below
        if len(positions) >= cfg.max_open_positions:
            break

        df = fetch_recent_bars(data_client, symbol, lookback_days=cfg.trend_window + 30)
        if df is None or len(df) < cfg.trend_window:
            continue

        df = compute_indicators(df, cfg)
        last_row = df.iloc[-1]

        if not entry_signal(last_row, cfg):
            continue

        entry_price = float(last_row["close"])
        stop, target = stop_and_target(entry_price, float(last_row["atr"]), cfg)
        risk_per_share = entry_price - stop
        if risk_per_share <= 0:
            continue

        risk_amount = equity * cfg.risk_pct_per_trade
        shares = int(risk_amount // risk_per_share)
        if shares <= 0:
            continue

        # Bracket order: entry + attached stop-loss + take-profit, so exits are
        # enforced by Alpaca even if this script doesn't run again before they hit.
        order = MarketOrderRequest(
            symbol=symbol,
            qty=shares,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            order_class="bracket",
            stop_loss={"stop_price": round(stop, 2)},
            take_profit={"limit_price": round(target, 2)},
        )
        print(f"[paper] BUY {shares} {symbol} @ ~{entry_price:.2f} "
              f"stop={stop:.2f} target={target:.2f}")
        trading_client.submit_order(order)


if __name__ == "__main__":
    run_once()
