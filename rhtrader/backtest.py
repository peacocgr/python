"""Daily-bar backtester for the SMA crossover strategy.

Execution model (no look-ahead):
  * signals are computed on each day's close;
  * orders fill at the *next* day's open, adjusted by slippage;
  * a position is entered when the signal turns on and fully exited when it
    turns off. Positions are not topped up or trimmed in between, which is
    exactly what the live trader does.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

from .config import Config
from .data import align
from .metrics import summarize
from .strategy import SmaCrossover


@dataclass
class Trade:
    date: pd.Timestamp
    symbol: str
    side: str
    quantity: float
    price: float


@dataclass
class BacktestResult:
    equity: pd.Series
    benchmark: pd.Series
    trades: list[Trade] = field(default_factory=list)

    @property
    def stats(self) -> dict[str, float]:
        s = summarize(self.equity)
        s["trades"] = len(self.trades)
        s["win_rate"] = self.win_rate()
        return s

    @property
    def benchmark_stats(self) -> dict[str, float]:
        return summarize(self.benchmark)

    def win_rate(self) -> float:
        """Fraction of completed round trips (buy then sell) that made money."""
        entries: dict[str, Trade] = {}
        wins = total = 0
        for t in self.trades:
            if t.side == "buy":
                entries[t.symbol] = t
            elif t.symbol in entries:
                total += 1
                wins += t.price > entries.pop(t.symbol).price
        return wins / total if total else float("nan")


def size_order(cash: float, price: float, fractional: bool) -> float:
    if price <= 0 or cash <= 0:
        return 0.0
    qty = cash / price
    return math.floor(qty * 1e6) / 1e6 if fractional else float(math.floor(qty))


def run_backtest(bars: dict[str, pd.DataFrame], cfg: Config) -> BacktestResult:
    strategy = SmaCrossover(cfg.strategy.fast, cfg.strategy.slow)
    bars = align({s: bars[s] for s in cfg.symbols})
    dates = next(iter(bars.values())).index
    if len(dates) <= strategy.warmup + 1:
        raise ValueError(
            f"need more than {strategy.warmup + 1} common bars, got {len(dates)}"
        )

    opens = pd.DataFrame({s: df["open"] for s, df in bars.items()})
    closes = pd.DataFrame({s: df["close"] for s, df in bars.items()})
    signals = pd.DataFrame({s: strategy.signals(closes[s]) for s in cfg.symbols})

    slip = cfg.execution.slippage_bps / 10_000
    fee = cfg.execution.commission
    weight = cfg.position_weight

    # Start on the first bar with a valid signal so strategy and benchmark
    # cover the same period.
    start = strategy.warmup - 1
    cash = cfg.broker.paper_starting_cash
    shares = {s: 0.0 for s in cfg.symbols}
    trades: list[Trade] = []
    equity = {dates[start]: cash}

    for i in range(start + 1, len(dates)):
        day = dates[i]
        prev = signals.iloc[i - 1]
        open_px = opens.iloc[i]
        equity_at_open = cash + sum(shares[s] * open_px[s] for s in cfg.symbols)

        # Sells first so their proceeds are available for buys.
        for s in cfg.symbols:
            if prev[s] == 0 and shares[s] > 0:
                px = open_px[s] * (1 - slip)
                cash += shares[s] * px - fee
                trades.append(Trade(day, s, "sell", shares[s], px))
                shares[s] = 0.0
        for s in cfg.symbols:
            if prev[s] == 1 and shares[s] == 0:
                px = open_px[s] * (1 + slip)
                budget = min(equity_at_open * weight, cash - fee)
                qty = size_order(budget, px, cfg.portfolio.fractional)
                if qty > 0:
                    cash -= qty * px + fee
                    shares[s] = qty
                    trades.append(Trade(day, s, "buy", qty, px))

        equity[day] = cash + sum(shares[s] * closes.iloc[i][s] for s in cfg.symbols)

    equity_curve = pd.Series(equity, name="strategy")
    period = closes.iloc[start:]
    benchmark = (period / period.iloc[0]).mean(axis=1) * cfg.broker.paper_starting_cash
    return BacktestResult(equity_curve, benchmark.rename("buy_and_hold"), trades)
