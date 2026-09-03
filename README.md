# Trend-Filtered Mean-Reversion Bot (Paper Trading Only)

A swing-trading bot built around **expectancy, not win rate**. It buys short-term
pullbacks *only* within stocks that are already in an established uptrend, sizes
positions by volatility-adjusted risk, and reports win rate alongside expectancy,
average win/loss, and drawdown — never win rate alone.

## Strategy in one paragraph

Only consider a stock when its price is above its 200-day moving average (trend
filter). Within that uptrend, enter when RSI(14) drops below 30 or price touches
the 20-day moving average (pullback trigger). Stop-loss and take-profit are both
sized off ATR (volatility), not a fixed percentage. Positions are sized to risk a
fixed 1% of account equity per trade, with a cap on total open risk across the
portfolio so a cluster of correlated pullbacks can't quietly become one oversized bet.

Full rationale for these choices is in `strategy/signals.py` docstrings and was
discussed in the chat that produced this project — the short version: win rate
alone is a misleading target, so exits are designed to make average wins bigger
than average losses even if wins happen less than half the time.

## Project structure

```
config.py                    Strategy + universe parameters — tune here first
strategy/indicators.py       SMA, RSI, ATR (pure functions, no data dependency)
strategy/signals.py          Entry/exit logic — SAME code used by backtest and live
data/loader.py                yfinance loader (real data) + synthetic generator (testing)
backtest/engine.py           Day-by-day backtest engine, position sizing, risk caps
backtest/metrics.py          Win rate / expectancy / drawdown reporting
run_backtest.py              CLI entry point for backtests
live/alpaca_paper_trader.py  Live PAPER execution via Alpaca (run locally, see below)
```

## ⚠️ A note on where to run this

This project was built in a sandboxed environment that can't reach financial
data APIs (Yahoo Finance, Alpaca) — only package registries. So it's been
validated with **synthetic (fake) data** to confirm the code runs correctly
end to end, not with real market history. Treat any results you saw from the
synthetic run as a code check, not a strategy result.

**Run the real backtest and any live paper trading locally**, where you have
normal internet access and control of your own API keys.

## Getting started (run these locally)

```bash
pip install -r requirements.txt

# Real backtest, real historical data:
python run_backtest.py --source yfinance --start 2019-01-01 --end 2026-08-01
```

This prints a report (win rate, expectancy in $ and in R-multiples, max
drawdown, Sharpe) and saves `trade_log.csv` + `equity_curve.csv` for you to
inspect every trade.

### Before trusting any backtest result

- **Walk-forward it.** Tune `config.py` on one period (e.g. 2015–2021), then
  test unchanged on a later period it never saw (e.g. 2022–2026). A strategy
  that only works on the period it was tuned on isn't a strategy, it's a
  curve fit.
- **Check expectancy, not just win rate.** The report prints both on purpose.
- **Look at `exit_reasons` in the report.** If almost everything exits on
  `"stop"`, the entry logic isn't finding real pullback-in-uptrend setups.
  If almost everything exits on `"time"`, most trades are going nowhere.

### Going live (paper only)

1. Create a free Alpaca account, generate **paper** API keys.
2. `export APCA_API_KEY_ID=...` and `export APCA_API_SECRET_KEY=...`
   (never commit these or paste them into a chat — use env vars or a
   gitignored `.env` file)
3. `pip install alpaca-py`
4. `python live/alpaca_paper_trader.py` — intended to run once per trading
   day, shortly after market open. It imports the exact same signal logic
   used in the backtest, so live behavior matches what you tested.
5. To run it continuously without keeping a laptop open: schedule it — a
   cron job on a small VM, a scheduled GitHub Action, or a Claude Code /
   Cowork scheduled task that runs the script daily.

`alpaca_paper_trader.py` places **bracket orders** (entry + attached
stop-loss + take-profit), so your stop and target are enforced by Alpaca
even if the script isn't running at the exact moment they're hit.

## Tuning

Everything lives in `config.py`: trend window, pullback trigger, ATR
multiples for stop/target, risk per trade, max positions, portfolio heat
cap, and the stock universe. Change one thing at a time and re-run the
backtest so you know what moved the result.

## What this does *not* do

- Does not place live (real-money) trades — `alpaca_paper_trader.py` is
  hard-pointed at Alpaca's paper endpoint.
- Does not guarantee the strategy is profitable on real markets — a
  favorable backtest, even a walk-forward one, is evidence, not proof.
- Does not account for every real-world friction (order rejections, partial
  fills, corporate actions, delisted tickers) — treat this as a solid
  starting framework, not a finished production system.
