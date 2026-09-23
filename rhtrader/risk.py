"""Pre-trade risk checks. Every order passes through here before submission."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

from .backtest import size_order
from .broker import Order
from .config import Config


@dataclass
class RiskDecision:
    approved: list[Order] = field(default_factory=list)
    rejected: list[tuple[Order, str]] = field(default_factory=list)
    halted: str | None = None


def kill_switch_engaged(cfg: Config) -> bool:
    return Path(cfg.risk.kill_switch_file).exists()


def check_orders(
    orders: list[Order], cfg: Config, positions: dict[str, float]
) -> RiskDecision:
    decision = RiskDecision()

    if kill_switch_engaged(cfg):
        decision.halted = f"kill switch file '{cfg.risk.kill_switch_file}' exists"
        decision.rejected = [(o, decision.halted) for o in orders]
        return decision

    if len(orders) > cfg.risk.max_orders_per_run:
        decision.halted = (
            f"{len(orders)} orders exceeds max_orders_per_run="
            f"{cfg.risk.max_orders_per_run}"
        )
        decision.rejected = [(o, decision.halted) for o in orders]
        return decision

    for order in orders:
        if order.symbol not in cfg.symbols:
            decision.rejected.append((order, "symbol not in configured universe"))
        elif order.quantity <= 0:
            decision.rejected.append((order, "non-positive quantity"))
        elif order.side == "sell" and order.quantity > positions.get(order.symbol, 0) + 1e-9:
            decision.rejected.append((order, "sell exceeds held shares (no shorting)"))
        elif order.side == "buy" and order.notional > cfg.risk.max_order_notional:
            # Shrink oversized buys to the cap rather than skip them outright.
            # Exits are never capped: selling what we hold only reduces risk.
            clipped = _clip_quantity(order, cfg)
            if clipped.quantity > 0:
                decision.approved.append(clipped)
            else:
                decision.rejected.append((order, "exceeds max_order_notional"))
        else:
            decision.approved.append(order)
    return decision


def _clip_quantity(order: Order, cfg: Config) -> Order:
    price = max(order.ref_price, order.limit_price or 0.0)
    qty = size_order(cfg.risk.max_order_notional, price, cfg.portfolio.fractional)
    return replace(order, quantity=qty, reason=order.reason + " (clipped to max_order_notional)")
