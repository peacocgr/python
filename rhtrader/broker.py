"""Brokers: a local paper-trading simulator and a live Robinhood (OAuth/MCP) adapter.

Both expose the same small interface used by the trader:

    account()           -> Account(equity, cash, positions)
    latest_price(sym)   -> float | None
    open_order_symbols() -> set[str]
    submit(order)       -> dict describing the result
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse


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


OPEN_ORDER_STATES = {"new", "queued", "confirmed", "unconfirmed", "partially_filled"}


class RobinhoodBroker:
    """Live trading through Robinhood's Agentic Trading MCP server (OAuth).

    ``client`` is a connected :class:`rhtrader.robinhood_mcp.RobinhoodMCP`
    (anything with ``call(tool, arguments) -> data`` works, which keeps this
    testable). Orders can only go to the account you enabled for agents.
    """

    def __init__(self, client, account_number: str, review: bool = True):
        self.client = client
        self.account_number = account_number
        self.review = review

    def _pages(self, tool: str, key: str, **args) -> list[dict]:
        items: list[dict] = []
        args = {"account_number": self.account_number, **args}
        for _ in range(50):
            data = self.client.call(tool, args)
            items.extend(data.get(key) or [])
            cursor = _next_cursor(data.get("next"))
            if not cursor:
                break
            args["cursor"] = cursor
        return items

    def latest_price(self, symbol: str) -> float | None:
        data = self.client.call("get_equity_quotes", {"symbols": [symbol]})
        for r in data.get("results", []):
            q = r.get("quote") or {}
            if q.get("symbol") == symbol and q.get("state", "active") == "active":
                px = q.get("last_trade_price")
                return float(px) if px else None
        return None

    def account(self) -> Account:
        p = self.client.call("get_portfolio", {"account_number": self.account_number})
        bp = p.get("buying_power") or {}
        # Never use margin: spend at most the smallest cash-like figure.
        spendable = [
            float(v)
            for v in (p.get("cash"), bp.get("buying_power"), bp.get("unleveraged_buying_power"))
            if v is not None
        ]
        positions: dict[str, float] = {}
        for pos in self._pages("get_equity_positions", "positions"):
            qty = float(pos.get("quantity") or 0)
            if qty > 0 and pos.get("type", "long") == "long":
                positions[pos["symbol"]] = qty
        return Account(float(p["total_value"]), min(spendable, default=0.0), positions)

    def open_order_symbols(self) -> set[str]:
        since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        orders = self._pages("get_equity_orders", "orders", created_at_gte=since)
        return {o["symbol"] for o in orders if o.get("state") in OPEN_ORDER_STATES}

    def submit(self, order: Order) -> dict:
        args = {
            "account_number": self.account_number,
            "symbol": order.symbol,
            "side": order.side,
            "quantity": _fmt_qty(order.quantity),
            "time_in_force": "gfd",
            "market_hours": "regular_hours",
        }
        if order.limit_price is None:
            args["type"] = "market"
        else:
            args["type"] = "limit"
            args["limit_price"] = f"{order.limit_price:.2f}"

        if self.review:
            review = self.client.call("review_equity_order", args)
            alerts = _find_alerts(review)
            if alerts:
                return {"status": "skipped", "reason": "pre-trade review alerts", "alerts": alerts}

        # Same ref_id on retry so Robinhood de-duplicates a resent order.
        args["ref_id"] = str(uuid.uuid4())
        try:
            resp = self.client.call("place_equity_order", args)
        except (TimeoutError, ConnectionError):
            resp = self.client.call("place_equity_order", args)
        placed = resp.get("order", resp) if isinstance(resp, dict) else {}
        if not placed.get("id"):
            return {"status": "error", "response": resp}
        return {"status": placed.get("state", "submitted"), "order_id": placed["id"]}


def _fmt_qty(qty: float) -> str:
    return f"{qty:.6f}".rstrip("0").rstrip(".")


def _next_cursor(next_url) -> str | None:
    if not next_url:
        return None
    values = parse_qs(urlparse(str(next_url)).query).get("cursor")
    return values[0] if values else None


def _find_alerts(obj) -> list:
    """Collect every non-empty value under a key mentioning 'alert'."""
    found: list = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if "alert" in k.lower() and v:
                found.extend(v if isinstance(v, list) else [v])
            else:
                found.extend(_find_alerts(v))
    elif isinstance(obj, list):
        for v in obj:
            found.extend(_find_alerts(v))
    return found
