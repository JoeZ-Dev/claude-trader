"""
Event-driven (day-by-day) backtest engine.

Deliberately not vectorized: a day-by-day loop is much easier to audit for
lookahead bias than a clever vectorized version.

Execution model:
  - Signals are computed from day T's close/indicators.
  - Entries fill at day T+1's OPEN (what a once-a-day live trader can actually
    achieve), not at the signal-day close.
  - Stops/targets are checked intrabar against the daily high/low during the
    hold. If the bar's OPEN has already gapped past the level, the fill is the
    open price, not the (unreachable) exact stop/target.
  - The time exit counts trading days (bars) held, not calendar days.
  - If a point-in-time membership set is supplied, a symbol is only eligible
    for a NEW entry while it was actually in the index; open positions are
    allowed to ride out even if the name later leaves.
"""
from dataclasses import dataclass
from typing import Dict, List, Optional
import math
import pandas as pd

from config import StrategyConfig
from strategy.signals import compute_indicators, entry_signal, stop_and_target


@dataclass
class Trade:
    symbol: str
    entry_date: pd.Timestamp
    entry_price: float
    stop_price: float
    target_price: float
    shares: int
    exit_date: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    bars_held: int = 0  # trading days elapsed since the fill

    @property
    def is_open(self) -> bool:
        return self.exit_price is None

    @property
    def risk_per_share(self) -> float:
        return self.entry_price - self.stop_price

    @property
    def pnl(self) -> Optional[float]:
        if self.exit_price is None:
            return None
        return (self.exit_price - self.entry_price) * self.shares

    @property
    def r_multiple(self) -> Optional[float]:
        """PnL expressed in multiples of initial risk — the unit expectancy is measured in."""
        if self.exit_price is None or self.risk_per_share <= 0:
            return None
        return (self.exit_price - self.entry_price) / self.risk_per_share


class Backtester:
    def __init__(
        self,
        data: Dict[str, pd.DataFrame],
        cfg: StrategyConfig,
        membership=None,
        trade_start=None,
    ):
        self.cfg = cfg
        self.data = {sym: compute_indicators(df, cfg) for sym, df in data.items()}
        self.membership = membership
        self.trade_start = pd.Timestamp(trade_start) if trade_start is not None else None
        self.cash = cfg.starting_equity
        self.open_positions: Dict[str, Trade] = {}
        self.pending_entries: Dict[str, dict] = {}  # sym -> {"signal_date", "atr"}
        self.closed_trades: List[Trade] = []
        self.equity_curve: List[tuple] = []  # (date, equity)

    # ---- helpers -------------------------------------------------
    def _slip(self, price: float, buying: bool) -> float:
        factor = self.cfg.slippage_bps / 10_000
        return price * (1 + factor) if buying else price * (1 - factor)

    def _current_open_risk(self) -> float:
        return sum(t.risk_per_share * t.shares for t in self.open_positions.values())

    def _mark_to_market(self, date, day_rows: Dict[str, pd.Series]) -> float:
        equity = self.cash
        for sym, trade in self.open_positions.items():
            if sym in day_rows:
                equity += day_rows[sym]["close"] * trade.shares
            else:
                equity += trade.entry_price * trade.shares  # stale price fallback
        return equity

    def _eligible(self, sym: str, date) -> bool:
        if self.membership is None:
            return True
        return sym in self.membership.members_asof(date)

    # ---- main loop -------------------------------------------------
    def run(self) -> Dict:
        all_dates = sorted(set().union(*[df.index for df in self.data.values()]))

        for date in all_dates:
            trading = self.trade_start is None or date >= self.trade_start
            day_rows = {
                sym: df.loc[date] for sym, df in self.data.items() if date in df.index
            }

            # 1) Manage open positions: stop / target / time exit
            for sym in list(self.open_positions.keys()):
                trade = self.open_positions[sym]
                trade.bars_held += 1
                if sym not in day_rows:
                    continue
                row = day_rows[sym]
                o = row["open"]
                exit_price, reason = None, None

                if o <= trade.stop_price:                 # gapped down through the stop
                    exit_price, reason = o, "stop"
                elif o >= trade.target_price:             # gapped up through the target
                    exit_price, reason = o, "target"
                elif row["low"] <= trade.stop_price:      # stop touched intrabar
                    exit_price, reason = trade.stop_price, "stop"
                elif row["high"] >= trade.target_price:   # target touched intrabar
                    exit_price, reason = trade.target_price, "target"
                elif trade.bars_held >= self.cfg.max_hold_days:
                    exit_price, reason = row["close"], "time"

                if exit_price is not None:
                    filled = self._slip(exit_price, buying=False)
                    trade.exit_date, trade.exit_price, trade.exit_reason = date, filled, reason
                    self.cash += filled * trade.shares
                    self.closed_trades.append(trade)
                    del self.open_positions[sym]

            equity_now = self._mark_to_market(date, day_rows)

            # 2) Fill entries signalled on the PREVIOUS bar, at today's open
            for sym in list(self.pending_entries.keys()):
                if sym not in day_rows:
                    continue  # no bar today; try again next session
                pend = self.pending_entries[sym]
                del self.pending_entries[sym]

                if sym in self.open_positions:
                    continue
                if len(self.open_positions) >= self.cfg.max_open_positions:
                    continue
                if not self._eligible(sym, date):
                    continue

                row = day_rows[sym]
                entry_price = self._slip(row["open"], buying=True)
                stop, target = stop_and_target(entry_price, pend["atr"], self.cfg)
                risk_per_share = entry_price - stop
                if risk_per_share <= 0:
                    continue

                risk_amount = equity_now * self.cfg.risk_pct_per_trade
                shares = math.floor(risk_amount / risk_per_share)
                if shares <= 0:
                    continue

                proposed_risk = risk_per_share * shares
                if self._current_open_risk() + proposed_risk > equity_now * self.cfg.max_portfolio_heat:
                    continue  # would blow through the portfolio-heat cap

                cost = entry_price * shares + self.cfg.commission_per_share * shares
                if cost > self.cash:
                    continue

                self.cash -= cost
                self.open_positions[sym] = Trade(
                    symbol=sym, entry_date=date, entry_price=entry_price,
                    stop_price=stop, target_price=target, shares=shares,
                )

            # 3) Scan today's closes for NEW signals -> queue for next bar's open
            if trading:
                for sym, row in day_rows.items():
                    if sym in self.open_positions or sym in self.pending_entries:
                        continue
                    if not self._eligible(sym, date):
                        continue
                    if entry_signal(row, self.cfg):
                        self.pending_entries[sym] = {"signal_date": date, "atr": row["atr"]}

            if trading:
                self.equity_curve.append((date, self._mark_to_market(date, day_rows)))

        # Close anything still open at the end, at last known price
        for sym, trade in list(self.open_positions.items()):
            last_price = self.data[sym]["close"].iloc[-1]
            trade.exit_date = self.data[sym].index[-1]
            trade.exit_price = self._slip(last_price, buying=False)
            trade.exit_reason = "end_of_backtest"
            self.cash += trade.exit_price * trade.shares
            self.closed_trades.append(trade)
        self.open_positions.clear()

        return {
            "closed_trades": self.closed_trades,
            "equity_curve": pd.DataFrame(self.equity_curve, columns=["date", "equity"]).set_index("date"),
        }
