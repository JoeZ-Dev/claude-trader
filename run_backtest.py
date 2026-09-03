"""
Run a backtest.

Examples:
  # Sandbox/no-network validation run (fake data, just proves the code works):
  python run_backtest.py --source synthetic

  # Real backtest (run this locally, where Yahoo Finance is reachable):
  python run_backtest.py --source yfinance --start 2016-01-01 --end 2026-08-01

--start / --end define the TRADE window. Data is always loaded from
DATA_START so the 200-day trend filter is already warm on --start, but no
trade or equity point is recorded before --start. This keeps walk-forward
splits honest: each window trades its own regime from day one instead of
losing its first ~10 months to indicator warmup.
"""
import argparse

import pandas as pd

from config import StrategyConfig, UniverseConfig
from data.loader import load_yfinance, load_synthetic, load_pit_membership
from backtest.engine import Backtester
from backtest.metrics import print_report

# Fixed data span so every run (full period and each walk-forward window)
# shares byte-identical underlying data and stays directly comparable.
DATA_START = "2014-09-01"
DATA_END = "2026-08-01"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=["yfinance", "synthetic"], default="synthetic")
    parser.add_argument("--start", default="2016-01-01", help="first day trades/equity are counted")
    parser.add_argument("--end", default="2026-08-01")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    strat_cfg = StrategyConfig()
    universe = UniverseConfig()

    membership = load_pit_membership(universe.constituents_file)
    data_end = min(pd.Timestamp(args.end), pd.Timestamp(DATA_END))
    download_symbols = membership.union(DATA_START, data_end)

    print(f"Loading data ({args.source}) for {len(download_symbols)} point-in-time "
          f"S&P 500 members, data {DATA_START}..{data_end.date()}, "
          f"trades counted from {args.start}...")

    if args.source == "yfinance":
        data = load_yfinance(download_symbols, DATA_START, str(data_end.date()))
    else:
        print("[NOTE] Using SYNTHETIC data — this validates the code runs correctly,")
        print("       it is NOT evidence the strategy works on real markets.")
        data = load_synthetic(download_symbols, DATA_START, str(data_end.date()), seed=args.seed)

    if not data:
        print("No data loaded, aborting.")
        return

    # Trim to the requested end (warmup before --start is kept on purpose).
    data = {s: df[df.index <= data_end] for s, df in data.items()}
    data = {s: df for s, df in data.items() if not df.empty}

    bt = Backtester(data, strat_cfg, membership=membership, trade_start=args.start)
    results = bt.run()

    print_report(results["closed_trades"], results["equity_curve"], strat_cfg.starting_equity)

    # Save a trade log for inspection
    trade_rows = [
        {
            "symbol": t.symbol, "entry_date": t.entry_date, "entry_price": round(t.entry_price, 2),
            "exit_date": t.exit_date, "exit_price": round(t.exit_price, 2) if t.exit_price else None,
            "shares": t.shares, "bars_held": t.bars_held, "exit_reason": t.exit_reason,
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
