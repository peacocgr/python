"""Brokers: a local paper-trading simulator and a live Robinhood adapter.

Both expose the same small interface used by the trader:

    account()           -> Account(equity, cash, positions)
    latest_price(sym)   -> float | None
    open_order_symbols() -> set[str]
    submit(order)       -> dict describing the result
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class Order:
    symbol: str
    side: str  # "buy" or "sell"
    quantity: float
    ref_price: float  # last price the order was sized against
    limit_price: float | None  # None = market order (fractional only)
    reason: str = ""

    @property
    def notional(self) -> float:
        return self.quantity * max(self.ref_price, self.limit_price or 0.0)


@dataclass
class Account:
    equity: float
    cash: float
    positions: dict[str, float] = field(default_factory=dict)


class PaperBroker:
    """Simulated account persisted to a JSON file.

    Orders fill immediately at the reference price plus slippage, mirroring
    the backtest's cost model. A limit order the price doesn't reach stays
    unfilled.
    """

    def __init__(
        self,
        state_path: str | Path,
        starting_cash: float,
        prices: dict[str, float],
        slippage_bps: float = 0.0,
        commission: float = 0.0,
    ):
        self.path = Path(state_path)
        self.prices = prices
        self.slip = slippage_bps / 10_000
        self.commission = commission
        if self.path.exists():
            self.state = json.loads(self.path.read_text())
        else:
            self.state = {"cash": starting_cash, "positions": {}, "fills": []}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state, indent=2))

    def latest_price(self, symbol: str) -> float | None:
        return self.prices.get(symbol)

    def account(self) -> Account:
        positions = {s: q for s, q in self.state["positions"].items() if q > 0}
        value = sum(q * self.prices[s] for s, q in positions.items())
        return Account(self.state["cash"] + value, self.state["cash"], positions)

    def open_order_symbols(self) -> set[str]:
        return set()

    def submit(self, order: Order) -> dict:
        ref = self.prices[order.symbol]
        held = self.state["positions"].get(order.symbol, 0.0)
        if order.side == "buy":
            px = ref * (1 + self.slip)
            if order.limit_price is not None and px > order.limit_price:
                return {"status": "unfilled", "reason": "price above limit"}
            cost = order.quantity * px + self.commission
            if cost > self.state["cash"] + 1e-9:
                return {"status": "rejected", "reason": "insufficient cash"}
            self.state["cash"] -= cost
            self.state["positions"][order.symbol] = held + order.quantity
        else:
            if order.quantity > held + 1e-9:
                return {"status": "rejected", "reason": "insufficient shares"}
            px = ref * (1 - self.slip)
            if order.limit_price is not None and px < order.limit_price:
                return {"status": "unfilled", "reason": "price below limit"}
            self.state["cash"] += order.quantity * px - self.commission
            self.state["positions"][order.symbol] = held - order.quantity

        fill = {
            "time": datetime.now(timezone.utc).isoformat(),
            **asdict(order),
            "fill_price": round(px, 4),
        }
        self.state["fills"].append(fill)
        self._save()
        return {"status": "filled", "fill_price": round(px, 4)}


class RobinhoodBroker:
    """Live trading through a logged-in ``robin_stocks.robinhood`` module."""

    def __init__(self, rh):
        self.rh = rh

    def latest_price(self, symbol: str) -> float | None:
        prices = self.rh.stocks.get_latest_price(symbol, includeExtendedHours=False)
        return float(prices[0]) if prices and prices[0] else None

    def account(self) -> Account:
        portfolio = self.rh.profiles.load_portfolio_profile()
        equity = portfolio.get("equity") or portfolio.get("extended_hours_equity")
        # On margin accounts buying_power includes borrowing; never use margin.
        profile = self.rh.profiles.load_account_profile()
        cash = min(float(profile["cash"]), float(profile["buying_power"]))
        holdings = self.rh.account.build_holdings()
        positions = {
            sym: float(h["quantity"])
            for sym, h in holdings.items()
            if float(h["quantity"]) > 0
        }
        return Account(float(equity), cash, positions)

    def open_order_symbols(self) -> set[str]:
        symbols = set()
        for o in self.rh.orders.get_all_open_stock_orders() or []:
            symbols.add(self.rh.stocks.get_symbol_by_url(o["instrument"]))
        return symbols

    def submit(self, order: Order) -> dict:
        o = self.rh.orders
        if order.limit_price is None:
            place = (
                o.order_buy_fractional_by_quantity
                if order.side == "buy"
                else o.order_sell_fractional_by_quantity
            )
            resp = place(
                order.symbol, order.quantity, timeInForce="gfd", extendedHours=False
            )
        else:
            place = o.order_buy_limit if order.side == "buy" else o.order_sell_limit
            resp = place(
                order.symbol,
                order.quantity,
                order.limit_price,
                timeInForce="gfd",
                extendedHours=False,
            )
        if not resp or "id" not in resp:
            return {"status": "error", "response": resp}
        return {"status": resp.get("state", "submitted"), "order_id": resp["id"]}
