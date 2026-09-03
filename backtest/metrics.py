"""
Reports win rate ALONGSIDE expectancy, avg win/loss, and drawdown —
deliberately never in isolation. A strategy is not "good" because its win
rate is high; it's good if its expectancy is positive after costs and its
drawdown is survivable.
"""
from typing import List
import numpy as np
import pandas as pd

from backtest.engine import Trade


def trade_stats(trades: List[Trade]) -> dict:
    if not trades:
        return {"num_trades": 0}

    pnls = [t.pnl for t in trades]
    r_multiples = [t.r_multiple for t in trades if t.r_multiple is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    win_rate = len(wins) / len(pnls) if pnls else 0.0
    avg_win = float(np.mean(wins)) if wins else 0.0
    avg_loss = float(np.mean(losses)) if losses else 0.0  # negative number
    expectancy_dollars = float(np.mean(pnls)) if pnls else 0.0
    expectancy_r = float(np.mean(r_multiples)) if r_multiples else 0.0

    exit_reasons = pd.Series([t.exit_reason for t in trades]).value_counts().to_dict()

    return {
        "num_trades": len(trades),
        "win_rate": round(win_rate, 4),
        "avg_win_$": round(avg_win, 2),
        "avg_loss_$": round(avg_loss, 2),
        "expectancy_$_per_trade": round(expectancy_dollars, 2),
        "expectancy_R_per_trade": round(expectancy_r, 3),
        "exit_reasons": exit_reasons,
    }


def equity_stats(equity_curve: pd.DataFrame, starting_equity: float,
                 first_trade_date=None) -> dict:
    if equity_curve.empty:
        return {}

    equity = equity_curve["equity"]
    total_return = equity.iloc[-1] / starting_equity - 1

    running_max = equity.cummax()
    drawdown = (equity - running_max) / running_max
    max_drawdown = drawdown.min()

    # Sharpe is measured from the first trade onward: including the flat
    # pre-first-trade warmup stretch would understate volatility and inflate it.
    sharpe_equity = equity
    if first_trade_date is not None:
        sharpe_equity = equity[equity.index >= pd.Timestamp(first_trade_date)]
    daily_returns = sharpe_equity.pct_change().dropna()
    sharpe = (
        (daily_returns.mean() / daily_returns.std()) * np.sqrt(252)
        if len(daily_returns) > 1 and daily_returns.std() > 0
        else 0.0
    )

    return {
        "final_equity": round(float(equity.iloc[-1]), 2),
        "total_return_pct": round(float(total_return) * 100, 2),
        "max_drawdown_pct": round(float(max_drawdown) * 100, 2),
        "sharpe_approx": round(float(sharpe), 2),
    }


def print_report(trades: List[Trade], equity_curve: pd.DataFrame, starting_equity: float):
    t_stats = trade_stats(trades)
    first_trade_date = min((t.entry_date for t in trades), default=None)
    e_stats = equity_stats(equity_curve, starting_equity, first_trade_date)

    print("=" * 50)
    print("BACKTEST REPORT")
    print("=" * 50)
    print(f"Trades taken:        {t_stats.get('num_trades')}")
    print(f"Win rate:            {t_stats.get('win_rate', 0) * 100:.1f}%")
    print(f"Avg win:             ${t_stats.get('avg_win_$', 0):,.2f}")
    print(f"Avg loss:            ${t_stats.get('avg_loss_$', 0):,.2f}")
    print(f"Expectancy / trade:  ${t_stats.get('expectancy_$_per_trade', 0):,.2f}  "
          f"({t_stats.get('expectancy_R_per_trade', 0):.2f}R)")
    print(f"Exit reasons:        {t_stats.get('exit_reasons')}")
    print("-" * 50)
    print(f"Final equity:        ${e_stats.get('final_equity', 0):,.2f}")
    print(f"Total return:        {e_stats.get('total_return_pct', 0):.2f}%")
    print(f"Max drawdown:        {e_stats.get('max_drawdown_pct', 0):.2f}%")
    print(f"Sharpe (approx):     {e_stats.get('sharpe_approx', 0):.2f}")
    print("=" * 50)
