import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from rhtrader.broker import Account, Order, PaperBroker, RobinhoodBroker
from rhtrader.config import load_config
from rhtrader.data import NEW_YORK, append_official_closes, drop_incomplete_bar, fetch_robinhood
from rhtrader.risk import check_orders
from rhtrader.trader import latest_signals, plan_orders, run_cycle

from .conftest import make_bars

UP = [10, 10, 10, 10, 10, 11, 12, 13, 14, 15]
DOWN = [15, 15, 15, 15, 15, 14, 13, 12, 11, 10]


def _after_close(bars):
    d = bars.index[-1]
    return datetime(d.year, d.month, d.day, 17, 0, tzinfo=NEW_YORK)


# --- data -----------------------------------------------------------------

def test_drop_incomplete_bar_during_session():
    bars = make_bars(UP)
    d = bars.index[-1]
    midday = datetime(d.year, d.month, d.day, 12, 0, tzinfo=NEW_YORK)
    assert len(drop_incomplete_bar(bars, midday)) == len(bars) - 1
    assert len(drop_incomplete_bar(bars, _after_close(bars))) == len(bars)


def test_stale_data_is_skipped(cfg):
    bars = {s: make_bars(UP) for s in cfg.symbols}
    later = datetime(2030, 1, 1, 17, tzinfo=NEW_YORK)
    signals, skipped = latest_signals(bars, cfg, later)
    assert signals == {}
    assert all("stale" in r for r in skipped.values())


# --- planning ---------------------------------------------------------------

def test_plan_buys_on_signal_and_sells_on_exit(cfg):
    acct = Account(equity=10_000, cash=5_000, positions={"BBB": 40})
    orders = plan_orders({"AAA": 1, "BBB": 0}, {"AAA": 100.0, "BBB": 125.0}, acct, cfg)
    by_sym = {o.symbol: o for o in orders}
    assert by_sym["BBB"].side == "sell" and by_sym["BBB"].quantity == 40
    assert by_sym["AAA"].side == "buy"
    # Target is 50% of equity, but only $5,000 cash is available.
    assert by_sym["AAA"].quantity * by_sym["AAA"].limit_price <= 5_000
    assert by_sym["AAA"].limit_price > 100.0
    assert by_sym["BBB"].limit_price < 125.0


def test_plan_is_idempotent_when_already_positioned(cfg):
    acct = Account(equity=10_000, cash=0, positions={"AAA": 10})
    assert plan_orders({"AAA": 1, "BBB": 0}, {"AAA": 100.0, "BBB": 50.0}, acct, cfg) == []


def test_fractional_orders_are_market_orders(cfg):
    cfg.portfolio.fractional = True
    acct = Account(equity=1_000, cash=1_000)
    (order, _) = plan_orders({"AAA": 1, "BBB": 1}, {"AAA": 333.0, "BBB": 333.0}, acct, cfg)
    assert order.limit_price is None
    assert order.quantity == pytest.approx(500 / 333.0, abs=1e-5)


# --- risk -------------------------------------------------------------------

def test_kill_switch_blocks_everything(cfg):
    Path(cfg.risk.kill_switch_file).touch()
    d = check_orders([Order("AAA", "buy", 1, 10, 10)], cfg, {})
    assert d.approved == [] and d.halted


def test_too_many_orders_halts(cfg):
    cfg.risk.max_orders_per_run = 1
    orders = [Order("AAA", "buy", 1, 10, 10), Order("BBB", "buy", 1, 10, 10)]
    assert check_orders(orders, cfg, {}).halted


def test_oversized_buy_is_clipped_including_market_orders(cfg):
    cfg.risk.max_order_notional = 1_000
    d = check_orders(
        [Order("AAA", "buy", 50, 100, 101), Order("BBB", "buy", 50, 100, None)], cfg, {}
    )
    assert [o.quantity for o in d.approved] == [9, 10]


def test_rejects_shorting_and_unknown_symbols(cfg):
    d = check_orders(
        [Order("AAA", "sell", 5, 10, 10), Order("ZZZ", "buy", 1, 10, 10)],
        cfg,
        {"AAA": 2},
    )
    assert d.approved == []
    assert len(d.rejected) == 2


# --- paper broker -----------------------------------------------------------

def test_paper_broker_persists_and_respects_limits(cfg):
    prices = {"AAA": 100.0}
    b = PaperBroker(cfg.broker.paper_state, 1_000, prices)
    assert b.submit(Order("AAA", "buy", 5, 100, 99))["status"] == "unfilled"
    assert b.submit(Order("AAA", "buy", 20, 100, 101))["status"] == "rejected"
    assert b.submit(Order("AAA", "buy", 5, 100, 101))["status"] == "filled"

    b2 = PaperBroker(cfg.broker.paper_state, 1_000, prices)
    acct = b2.account()
    assert acct.positions == {"AAA": 5}
    assert acct.cash == 500 and acct.equity == 1_000


# --- full cycle -------------------------------------------------------------

def test_run_cycle_dry_run_then_execute(cfg):
    bars = {"AAA": make_bars(UP), "BBB": make_bars(DOWN)}
    prices = {"AAA": 15.0, "BBB": 10.0}
    now = _after_close(bars["AAA"])

    broker = PaperBroker(cfg.broker.paper_state, 10_000, prices)
    report = run_cycle(cfg, broker, bars, dry_run=True, now=now)
    assert report.signals == {"AAA": 1, "BBB": 0}
    assert [o.symbol for o in report.risk.approved] == ["AAA"]
    assert report.results == []
    assert broker.account().positions == {}

    report = run_cycle(cfg, broker, bars, dry_run=False, now=now)
    assert report.results[0]["status"] == "filled"
    assert broker.account().positions["AAA"] > 0

    # Second run the same day does nothing.
    assert run_cycle(cfg, broker, bars, dry_run=False, now=now).planned == []
    lines = Path(cfg.trade_log).read_text().splitlines()
    assert len(lines) == 3 and json.loads(lines[1])["results"]


class FakeMCP:
    """Stands in for RobinhoodMCP; responses mirror Robinhood's tool payloads."""

    def __init__(self, review_alerts=None, positions=None):
        self.calls = []
        self.review_alerts = review_alerts or []
        self.positions = positions or []

    def call(self, tool, args=None):
        self.calls.append((tool, args))
        if tool == "get_equity_quotes":
            return {"results": [{"quote": {"symbol": s, "last_trade_price": "15.000000", "state": "active"}}
                                for s in args["symbols"]]}
        if tool == "get_portfolio":
            return {"total_value": "10000", "cash": "10000",
                    "buying_power": {"buying_power": "20000.0000", "unleveraged_buying_power": "10000.0000"}}
        if tool == "get_equity_positions":
            return {"positions": self.positions}
        if tool == "get_equity_orders":
            return {"orders": [{"symbol": "BBB", "state": "queued"}, {"symbol": "AAA", "state": "filled"}]}
        if tool == "review_equity_order":
            return {"quote": {}, "alerts": self.review_alerts}
        if tool == "place_equity_order":
            return {"id": "abc", "state": "queued"}
        raise AssertionError(f"unexpected tool {tool}")


def test_robinhood_broker_reviews_then_places_and_skips_pending(cfg):
    fake = FakeMCP()
    broker = RobinhoodBroker(fake, "ACCT1")
    bars = {"AAA": make_bars(UP), "BBB": make_bars(UP)}
    report = run_cycle(cfg, broker, bars, dry_run=False, now=_after_close(bars["AAA"]))
    assert "open order" in report.skipped["BBB"]
    tools = [t for t, _ in fake.calls]
    assert tools.index("review_equity_order") < tools.index("place_equity_order")
    placed = dict(fake.calls)["place_equity_order"]
    assert placed["account_number"] == "ACCT1"
    assert (placed["symbol"], placed["side"], placed["type"]) == ("AAA", "buy", "limit")
    assert (placed["quantity"], placed["limit_price"], placed["time_in_force"]) == ("332", "15.03", "gfd")
    assert placed["ref_id"]
    assert report.results[0]["order_id"] == "abc"


def test_robinhood_broker_skips_orders_flagged_by_review(cfg):
    fake = FakeMCP(review_alerts=[{"type": "buying_power", "message": "insufficient"}])
    broker = RobinhoodBroker(fake, "ACCT1")
    result = broker.submit(Order("AAA", "buy", 1, 15, 15.03))
    assert result["status"] == "skipped" and result["alerts"]
    assert "place_equity_order" not in [t for t, _ in fake.calls]


def test_robinhood_account_never_uses_margin():
    fake = FakeMCP(positions=[{"symbol": "AAA", "quantity": "3.0000", "type": "long"}])
    acct = RobinhoodBroker(fake, "ACCT1").account()
    assert acct.cash == 10_000  # not the 20k margin buying power
    assert acct.positions == {"AAA": 3.0}


def test_fetch_robinhood_parses_bars_and_drops_interpolated():
    bar = lambda d, px, interp=False: {"begins_at": f"{d}T00:00:00Z", "open_price": str(px),
                                        "high_price": str(px), "low_price": str(px),
                                        "close_price": str(px), "volume": 10, "interpolated": interp}
    fake = SimpleNamespace(call=lambda tool, args: {"results": [
        {"symbol": "AAA", "bars": [bar("2026-01-02", 1), bar("2026-01-03", 9, True), bar("2026-01-05", 2)]}
    ]})
    df = fetch_robinhood(fake, ["AAA"], datetime(2026, 1, 1, tzinfo=NEW_YORK))["AAA"]
    assert list(df["close"]) == [1.0, 2.0]


def test_official_close_fills_the_interpolated_last_day():
    bars = {"AAA": make_bars([10, 11, 12])}  # ends 2024-01-03
    quotes = {"results": [
        {"quote": {"symbol": "AAA"}, "close": {"symbol": "AAA", "date": "2024-01-04", "price": "13.5",
                                                 "interpolated": False}},
    ]}
    fake = SimpleNamespace(call=lambda tool, args: quotes)
    out = append_official_closes(fake, bars)["AAA"]
    assert out.index[-1] == pd.Timestamp("2024-01-04") and out["close"].iloc[-1] == 13.5
    # Already up to date or interpolated close: unchanged.
    assert len(append_official_closes(fake, {"AAA": out})["AAA"]) == 4
    quotes["results"][0]["close"].update(date="2024-01-05", interpolated=True)
    assert len(append_official_closes(fake, {"AAA": out})["AAA"]) == 4


# --- config -----------------------------------------------------------------

def test_example_config_loads():
    cfg = load_config(Path(__file__).parent.parent / "config.example.toml")
    assert cfg.broker.mode == "paper"


def test_live_mode_requires_account_number(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[broker]\nmode = "live"\n')
    with pytest.raises(ValueError, match="account_number"):
        load_config(p)


def test_config_rejects_unknown_keys(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[risk]\nmax_order_notionl = 5\n")
    with pytest.raises(ValueError, match="max_order_notionl"):
        load_config(p)
