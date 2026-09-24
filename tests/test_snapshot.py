import json
from datetime import datetime

import numpy as np
import pytest

from rhtrader.data import NEW_YORK
from rhtrader.snapshot import SnapshotClient, format_plan, plan_from_snapshot

from .conftest import make_bars


def _write_snapshot(tmp_path, closes, quote_price=None, positions=()):
    bars = make_bars(closes)
    last = bars.index[-1]
    tool_bars = [
        {"begins_at": f"{d.date()}T00:00:00Z", "open_price": str(c), "high_price": str(c),
         "low_price": str(c), "close_price": str(c), "volume": 1}
        for d, c in zip(bars.index, bars["close"])
    ]
    px = str(quote_price if quote_price is not None else closes[-1])
    files = {
        "get_equity_historicals": {"data": {"results": [
            {"symbol": s, "bars": tool_bars} for s in ("AAA", "BBB")]}, "guide": "..."},
        "get_equity_quotes": {"data": {"results": [
            {"quote": {"symbol": s, "last_trade_price": px, "state": "active"},
             "close": {"symbol": s, "date": str(last.date()), "price": str(closes[-1]),
                       "interpolated": False}} for s in ("AAA", "BBB")]}},
        "get_portfolio": {"data": {"total_value": "0", "cash": "0",
                                   "buying_power": {"buying_power": "0"}}},
        "get_equity_positions": {"data": {"positions": list(positions)}},
        "get_equity_orders": {"data": {"orders": []}},
    }
    for name, body in files.items():
        (tmp_path / f"{name}.json").write_text(json.dumps(body))
    return datetime(last.year, last.month, last.day, 17, tzinfo=NEW_YORK)


def test_snapshot_client_refuses_to_trade(tmp_path):
    client = SnapshotClient(tmp_path)
    for tool in ("review_equity_order", "place_equity_order", "cancel_equity_order"):
        with pytest.raises(PermissionError):
            client.call(tool, {})


def test_plan_outputs_place_order_args_with_hypothetical_cash(cfg, tmp_path):
    closes = list(np.linspace(10, 10, 5)) + [10, 11, 12, 13, 14, 15]
    now = _write_snapshot(tmp_path, closes)
    plan, _ = plan_from_snapshot(cfg, tmp_path, assume_cash=1_000, now=now)

    assert plan["symbols"]["AAA"]["signal"] == "LONG"
    args = [o["place_equity_order_args"] for o in plan["orders"]]
    assert {a["symbol"] for a in args} == {"AAA", "BBB"}
    a = args[0]
    assert a["type"] == "limit" and a["time_in_force"] == "gfd"
    assert float(a["quantity"]) * float(a["limit_price"]) <= 500
    assert "account_number" not in a and "ref_id" not in a
    assert "hypothetical" in format_plan(plan)


def test_plan_with_real_empty_account_places_nothing(cfg, tmp_path):
    now = _write_snapshot(tmp_path, [10] * 5 + [11, 12, 13, 14, 15])
    plan, _ = plan_from_snapshot(cfg, tmp_path, now=now)
    assert plan["orders"] == []


def test_plan_flags_crossover_and_sells_held_position(cfg, tmp_path):
    closes = [10] * 5 + [11, 12, 13, 14, 15, 15, 15, 15, 12, 9, 6]
    now = _write_snapshot(tmp_path, closes, positions=[{"symbol": "AAA", "quantity": "5", "type": "long"}])
    plan, _ = plan_from_snapshot(cfg, tmp_path, now=now)
    sym = plan["symbols"]["AAA"]
    assert sym["signal"] == "FLAT"
    sells = [o["place_equity_order_args"] for o in plan["orders"] if o["place_equity_order_args"]["side"] == "sell"]
    assert sells == [{"symbol": "AAA", "side": "sell", "quantity": "5", "time_in_force": "gfd",
                      "market_hours": "regular_hours", "type": "limit", "limit_price": "5.99"}]


def test_price_far_from_last_close_is_skipped(cfg, tmp_path):
    closes = [10] * 5 + [11, 12, 13, 14, 15]
    now = _write_snapshot(tmp_path, closes, quote_price=25)  # e.g. a mistyped quote
    plan, _ = plan_from_snapshot(cfg, tmp_path, assume_cash=1_000, now=now)
    assert plan["orders"] == []
    assert "from last close" in plan["symbols"]["AAA"]["skipped"]
