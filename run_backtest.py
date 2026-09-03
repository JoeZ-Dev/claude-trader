"""
Run a backtest.

Examples:
  # Sandbox/no-network validation run (fake data, just proves the code works):
  python run_backtest.py --source synthetic

  # Real backtest (run this locally, where Yahoo Finance is reachable):
  python run_backtest.py --source yfinance --start 2019-01-01 --end 2026-08-01
"""
import argparse

from config import StrategyConfig, UniverseConfig
from data.loader import load_yfinance, load_synthetic
from backtest.engine import Backtester
from backtest.metrics import print_report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=["yfinance", "synthetic"], default="synthetic")
    parser.add_argument("--start", default="2019-01-01")
    parser.add_argument("--end", default="2026-08-01")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    strat_cfg = StrategyConfig()
    universe = UniverseConfig()

    print(f"Loading data ({args.source}) for {len(universe.symbols)} symbols "
          f"from {args.start} to {args.end}...")

    if args.source == "yfinance":
        data = load_yfinance(universe.symbols, args.start, args.end)
    else:
        print("[NOTE] Using SYNTHETIC data — this validates the code runs correctly,")
        print("       it is NOT evidence the strategy works on real markets.")
        data = load_synthetic(universe.symbols, args.start, args.end, seed=args.seed)

    if not data:
        print("No data loaded, aborting.")
        return

    bt = Backtester(data, strat_cfg)
    results = bt.run()

    print_report(results["closed_trades"], results["equity_curve"], strat_cfg.starting_equity)

    # Save a trade log for inspection
    import pandas as pd
    trade_rows = [
        {
            "symbol": t.symbol, "entry_date": t.entry_date, "entry_price": round(t.entry_price, 2),
            "exit_date": t.exit_date, "exit_price": round(t.exit_price, 2) if t.exit_price else None,
            "shares": t.shares, "exit_reason": t.exit_reason,
            "pnl": round(t.pnl, 2) if t.pnl is not None else None,
            "r_multiple": round(t.r_multiple, 2) if t.r_multiple is not None else None,
        }
        for t in results["closed_trades"]
    ]
    pd.DataFrame(trade_rows).to_csv("trade_log.csv", index=False)
    results["equity_curve"].to_csv("equity_curve.csv")
    print("\nSaved: trade_log.csv, equity_curve.csv")


if __name__ == "__main__":
    main()
