"""One trading cycle: data -> signals -> target orders -> risk -> broker.

Designed to be run once per day after the close (e.g. from cron). It is
idempotent: re-running it the same day places no new orders, because it
only enters a position when none is held and only exits when one is.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .backtest import size_order
from .broker import Account, Order
from .config import Config
from .data import NEW_YORK, drop_incomplete_bar
from .risk import RiskDecision, check_orders
from .strategy import SmaCrossover


@dataclass
class CycleReport:
    signals: dict[str, int]
    prices: dict[str, float]
    account: Account
    planned: list[Order]
    risk: RiskDecision
    results: list[dict] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)


def latest_signals(
    bars: dict[str, pd.DataFrame],
    cfg: Config,
    now: datetime | None = None,
) -> tuple[dict[str, int], dict[str, str]]:
    """Signal from the last completed bar of each symbol, plus skip reasons."""
    now = (now or datetime.now(NEW_YORK)).astimezone(NEW_YORK)
    strategy = SmaCrossover(cfg.strategy.fast, cfg.strategy.slow)
    signals: dict[str, int] = {}
    skipped: dict[str, str] = {}
    for sym in cfg.symbols:
        df = drop_incomplete_bar(bars[sym], now)
        if len(df) < strategy.warmup:
            skipped[sym] = f"only {len(df)} bars, need {strategy.warmup}"
            continue
        age = (now.date() - df.index[-1].date()).days
        if age > cfg.risk.max_data_age_days:
            skipped[sym] = f"stale data: last bar {df.index[-1].date()} ({age} days old)"
            continue
        signals[sym] = int(strategy.signals(df["close"]).iloc[-1])
    return signals, skipped


def plan_orders(
    signals: dict[str, int],
    prices: dict[str, float],
    account: Account,
    cfg: Config,
) -> list[Order]:
    """Exit positions whose signal is off; enter positions whose signal is on."""
    buffer = cfg.execution.limit_buffer_bps / 10_000
    fractional = cfg.portfolio.fractional
    orders: list[Order] = []

    for sym, sig in signals.items():
        held = account.positions.get(sym, 0.0)
        if sig == 0 and held > 0:
            px = prices[sym]
            limit = None if fractional else round(px * (1 - buffer), 2)
            orders.append(Order(sym, "sell", held, px, limit, "fast SMA below slow SMA"))

    cash = account.cash
    target = account.equity * cfg.position_weight
    for sym, sig in signals.items():
        if sig == 1 and account.positions.get(sym, 0.0) == 0:
            px = prices[sym]
            limit = None if fractional else round(px * (1 + buffer), 2)
            budget = min(target, cash)
            qty = size_order(budget, limit or px, fractional)
            if qty > 0:
                orders.append(Order(sym, "buy", qty, px, limit, "fast SMA above slow SMA"))
                cash -= qty * (limit or px)
    return orders


def run_cycle(
    cfg: Config,
    broker,
    bars: dict[str, pd.DataFrame],
    *,
    dry_run: bool = True,
    now: datetime | None = None,
) -> CycleReport:
    signals, skipped = latest_signals(bars, cfg, now)

    prices: dict[str, float] = {}
    for sym in list(signals):
        px = broker.latest_price(sym)
        last_close = float(bars[sym]["close"].iloc[-1])
        if px is None or px <= 0:
            skipped[sym] = "no price available"
            signals.pop(sym)
        elif abs(px / last_close - 1) > cfg.risk.max_price_gap_pct and signals[sym] == 1:
            # Only new entries are blocked: after an earnings gap the exit is
            # exactly the order that must still go through.
            skipped[sym] = (
                f"price {px:,.2f} is more than {cfg.risk.max_price_gap_pct:.0%} "
                f"from last close {last_close:,.2f}; not buying, check the data"
            )
            signals.pop(sym)
        else:
            prices[sym] = px

    pending = broker.open_order_symbols()
    for sym in list(signals):
        if sym in pending:
            skipped[sym] = "has an open order; waiting for it to resolve"
            signals.pop(sym)

    account = broker.account()
    planned = plan_orders(signals, prices, account, cfg)
    decision = check_orders(planned, cfg, account.positions)
    report = CycleReport(signals, prices, account, planned, decision, skipped=skipped)

    if not dry_run:
        for order in decision.approved:
            try:
                result = broker.submit(order)
            except Exception as e:  # keep going; log the failure
                result = {"status": "error", "error": repr(e)}
            report.results.append({"order": asdict(order), **result})

    _log(cfg, report, dry_run)
    return report


def _log(cfg: Config, report: CycleReport, dry_run: bool) -> None:
    path = Path(cfg.trade_log)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "time": datetime.now(timezone.utc).isoformat(),
        "mode": cfg.broker.mode,
        "dry_run": dry_run,
        "signals": report.signals,
        "prices": report.prices,
        "equity": report.account.equity,
        "cash": report.account.cash,
        "positions": report.account.positions,
        "skipped": report.skipped,
        "halted": report.risk.halted,
        "approved": [asdict(o) for o in report.risk.approved],
        "rejected": [{"order": asdict(o), "reason": r} for o, r in report.risk.rejected],
        "results": report.results,
    }
    with path.open("a") as f:
        f.write(json.dumps(entry) + "\n")
