"""Plan trades from saved Robinhood tool responses.

This lets an agent that already has a Robinhood MCP connection (e.g. a
scheduled Claude task) gather read-only data, save each tool's JSON
response as ``<snapshot_dir>/<tool_name>.json``, and let rhtrader's tested
code decide the orders:

    get_equity_historicals.json   daily bars for every configured symbol
    get_equity_quotes.json        quotes (with official closes) for them
    get_portfolio.json            the agentic account's portfolio
    get_equity_positions.json     its positions
    get_equity_orders.json        its recent orders

The planner never places or reviews orders; it outputs them as the exact
arguments for ``place_equity_order``.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .broker import Account, RobinhoodBroker
from .config import Config
from .data import NEW_YORK, append_official_closes, drop_incomplete_bar, fetch_robinhood
from .robinhood_mcp import _parse_payload
from .strategy import SmaCrossover
from .trader import CycleReport, run_cycle

WRITE_TOOLS = {"review_equity_order", "place_equity_order", "cancel_equity_order"}


class SnapshotClient:
    """Answers tool calls from saved responses. Refuses anything that trades."""

    def __init__(self, directory: str | Path):
        self.dir = Path(directory)

    def call(self, tool: str, arguments: dict | None = None):
        if tool in WRITE_TOOLS:
            raise PermissionError(f"snapshot planning never calls {tool}")
        path = self.dir / f"{tool}.json"
        if not path.exists():
            raise FileNotFoundError(f"missing snapshot file {path}")
        text = path.read_text()
        payload = _parse_payload(text, None)
        if payload is None:
            raise ValueError(f"{path} is not valid JSON")
        return payload.get("data", payload) if isinstance(payload, dict) else payload


def plan_from_snapshot(
    cfg: Config,
    directory: str | Path,
    *,
    assume_cash: float | None = None,
    now: datetime | None = None,
) -> tuple[dict, CycleReport]:
    client = SnapshotClient(directory)
    bars = fetch_robinhood(client, cfg.symbols, datetime.now(NEW_YORK))
    bars = append_official_closes(client, bars)
    broker = RobinhoodBroker(client, cfg.robinhood.account_number or "snapshot")

    if assume_cash is not None:
        real_account = broker.account

        def hypothetical() -> Account:
            acct = real_account()
            return Account(assume_cash, assume_cash, acct.positions)

        broker.account = hypothetical

    report = run_cycle(cfg, broker, bars, dry_run=True, now=now)

    strategy = SmaCrossover(cfg.strategy.fast, cfg.strategy.slow)
    now_ny = (now or datetime.now(NEW_YORK)).astimezone(NEW_YORK)
    symbols = {}
    for sym in cfg.symbols:
        df = drop_incomplete_bar(bars[sym], now_ny)
        ind = strategy.indicators(df["close"])
        sig = strategy.signals(df["close"])
        symbols[sym] = {
            "as_of": str(df.index[-1].date()),
            "close": round(float(ind["close"].iloc[-1]), 2),
            "sma_fast": round(float(ind["sma_fast"].iloc[-1]), 2),
            "sma_slow": round(float(ind["sma_slow"].iloc[-1]), 2),
            "signal": "LONG" if sig.iloc[-1] else "FLAT",
            "previous_signal": "LONG" if sig.iloc[-2] else "FLAT",
            "crossed_today": bool(sig.iloc[-1] != sig.iloc[-2]),
            "price": report.prices.get(sym),
            "held": report.account.positions.get(sym, 0.0),
            "skipped": report.skipped.get(sym),
        }

    orders = []
    for o in report.risk.approved:
        args = {
            "symbol": o.symbol,
            "side": o.side,
            "quantity": f"{o.quantity:.6f}".rstrip("0").rstrip("."),
            "time_in_force": "gfd",
            "market_hours": "regular_hours",
        }
        if o.limit_price is None:
            args["type"] = "market"
        else:
            args.update(type="limit", limit_price=f"{o.limit_price:.2f}")
        orders.append({"place_equity_order_args": args, "reason": o.reason})

    plan = {
        "generated_at": now_ny.isoformat(timespec="seconds"),
        "strategy": f"SMA {cfg.strategy.fast}/{cfg.strategy.slow}",
        "hypothetical_cash": assume_cash,
        "account": {
            "equity": report.account.equity,
            "cash": report.account.cash,
            "positions": report.account.positions,
        },
        "symbols": symbols,
        "halted": report.risk.halted,
        "orders": orders,
        "rejected": [
            {"order": asdict(o), "reason": reason} for o, reason in report.risk.rejected
        ],
    }
    return plan, report


def format_plan(plan: dict) -> str:
    lines = [f"{plan['strategy']} plan, {plan['generated_at']}"]
    acct = plan["account"]
    label = " (hypothetical)" if plan["hypothetical_cash"] is not None else ""
    lines.append(f"Account{label}: equity ${acct['equity']:,.2f}, cash ${acct['cash']:,.2f}")
    for sym, s in plan["symbols"].items():
        cross = "  ** CROSSED TODAY **" if s["crossed_today"] else ""
        lines.append(
            f"  {sym:5} {s['signal']:4} close {s['close']:,.2f}  "
            f"SMAs {s['sma_fast']:,.2f} / {s['sma_slow']:,.2f}  held {s['held']:g}{cross}"
        )
        if s["skipped"]:
            lines.append(f"        skipped: {s['skipped']}")
    if plan["halted"]:
        lines.append(f"HALTED: {plan['halted']}")
    if not plan["orders"]:
        lines.append("No orders.")
    for o in plan["orders"]:
        a = o["place_equity_order_args"]
        price = f" limit {a['limit_price']}" if a["type"] == "limit" else " market"
        lines.append(f"  ORDER {a['side']} {a['quantity']} {a['symbol']}{price}  ({o['reason']})")
    for r in plan["rejected"]:
        lines.append(f"  REJECTED {r['order']['side']} {r['order']['symbol']}: {r['reason']}")
    return "\n".join(lines)


def write_plan(plan: dict, path: str | Path) -> None:
    Path(path).write_text(json.dumps(plan, indent=2))
