import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from rhtrader.broker import Account, Order, PaperBroker, RobinhoodBroker
from rhtrader.config import load_config
from rhtrader.data import NEW_YORK, drop_incomplete_bar
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


def _fake_rh(orders_placed):
    def place(side):
        def f(symbol, quantity, limitPrice, timeInForce, extendedHours):
            orders_placed.append((side, symbol, quantity, limitPrice, timeInForce))
            return {"id": "abc", "state": "queued"}
        return f

    return SimpleNamespace(
        stocks=SimpleNamespace(
            get_latest_price=lambda s, includeExtendedHours: ["15.00"],
            get_symbol_by_url=lambda url: url.rsplit("/", 1)[-1],
        ),
        profiles=SimpleNamespace(
            load_portfolio_profile=lambda: {"equity": "10000.00"},
            load_account_profile=lambda: {"cash": "10000.00", "buying_power": "20000.00"},
        ),
        account=SimpleNamespace(build_holdings=lambda: {}),
        orders=SimpleNamespace(
            get_all_open_stock_orders=lambda: [{"instrument": "https://x/BBB"}],
            order_buy_limit=place("buy"),
            order_sell_limit=place("sell"),
        ),
    )


def test_robinhood_broker_places_limit_orders_and_skips_pending(cfg):
    placed = []
    broker = RobinhoodBroker(_fake_rh(placed))
    bars = {"AAA": make_bars(UP), "BBB": make_bars(UP)}
    report = run_cycle(cfg, broker, bars, dry_run=False, now=_after_close(bars["AAA"]))
    assert "open order" in report.skipped["BBB"]
    assert placed == [("buy", "AAA", 332.0, 15.03, "gfd")]
    assert report.results[0]["order_id"] == "abc"


# --- config -----------------------------------------------------------------

def test_example_config_loads():
    cfg = load_config(Path(__file__).parent.parent / "config.example.toml")
    assert cfg.broker.mode == "paper"


def test_config_rejects_unknown_keys(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[risk]\nmax_order_notionl = 5\n")
    with pytest.raises(ValueError, match="max_order_notionl"):
        load_config(p)
