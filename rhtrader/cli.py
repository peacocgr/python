"""Command-line interface.

    python -m rhtrader backtest [--config FILE] [--fast N --slow N] [--start DATE]
    python -m rhtrader trade    [--config FILE] [--execute] [--live]
    python -m rhtrader status   [--config FILE]
    python -m rhtrader fetch-data [--config FILE] [--span 5year]

``trade`` is a dry run unless ``--execute`` is given. Real orders need all
three of: ``broker.mode = "live"`` in the config, ``--execute`` and ``--live``.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace

import pandas as pd

from .backtest import run_backtest
from .broker import PaperBroker, RobinhoodBroker
from .config import Config, Credentials, load_config
from .data import fetch_robinhood, load_csv
from .risk import kill_switch_engaged
from .trader import run_cycle


def _pct(x: float) -> str:
    return "n/a" if x != x else f"{x * 100:7.2f}%"


def _load_bars(cfg: Config, rh=None) -> dict[str, pd.DataFrame]:
    if cfg.data.source == "robinhood":
        if rh is None:
            from .robinhood_client import login

            rh = login(Credentials.from_env())
        return {s: fetch_robinhood(rh, s) for s in cfg.symbols}
    return {s: load_csv(cfg.data.csv_dir, s) for s in cfg.symbols}


def cmd_backtest(cfg: Config, args: argparse.Namespace) -> int:
    if args.fast or args.slow:
        cfg.strategy = replace(
            cfg.strategy,
            fast=args.fast or cfg.strategy.fast,
            slow=args.slow or cfg.strategy.slow,
        )
        cfg.validate()
    bars = _load_bars(cfg)
    if args.start:
        bars = {s: df.loc[args.start:] for s, df in bars.items()}
    result = run_backtest(bars, cfg)

    eq = result.equity
    print(
        f"SMA {cfg.strategy.fast}/{cfg.strategy.slow} on {', '.join(cfg.symbols)}  "
        f"{eq.index[0].date()} -> {eq.index[-1].date()}  "
        f"(start ${cfg.broker.paper_starting_cash:,.0f})\n"
    )
    s, b = result.stats, result.benchmark_stats
    print(f"{'':16}{'strategy':>12}{'buy & hold':>12}")
    for key, label in [
        ("total_return", "Total return"),
        ("cagr", "CAGR"),
        ("volatility", "Volatility"),
        ("max_drawdown", "Max drawdown"),
    ]:
        print(f"{label:16}{_pct(s[key]):>12}{_pct(b[key]):>12}")
    print(f"{'Sharpe':16}{s['sharpe']:>12.2f}{b['sharpe']:>12.2f}")
    print(f"{'Final equity':16}{eq.iloc[-1]:>12,.0f}{result.benchmark.iloc[-1]:>12,.0f}")
    print(f"{'Trades':16}{s['trades']:>12}")
    print(f"{'Win rate':16}{_pct(s['win_rate']):>12}")

    if args.trades:
        print()
        for t in result.trades:
            print(f"{t.date.date()}  {t.side:4} {t.quantity:>10g} {t.symbol:6} @ {t.price:,.2f}")
    if args.equity_csv:
        pd.concat([result.equity, result.benchmark], axis=1).to_csv(args.equity_csv)
        print(f"\nequity curves written to {args.equity_csv}")
    return 0


def cmd_trade(cfg: Config, args: argparse.Namespace) -> int:
    live = cfg.broker.mode == "live"
    if live and args.execute and not args.live:
        print("broker.mode is 'live': pass --live as well to place real orders.")
        return 2
    if args.live and not live:
        print("--live given but broker.mode is not 'live' in the config.")
        return 2
    if kill_switch_engaged(cfg):
        print(f"Kill switch engaged ({cfg.risk.kill_switch_file} exists). No orders.")

    rh = None
    if live or cfg.data.source == "robinhood":
        from .robinhood_client import login

        rh = login(Credentials.from_env())
    bars = _load_bars(cfg, rh)

    if live:
        broker = RobinhoodBroker(rh)
    else:
        if rh is not None:
            prices = {s: RobinhoodBroker(rh).latest_price(s) for s in cfg.symbols}
        else:
            prices = {s: float(df["close"].iloc[-1]) for s, df in bars.items()}
        broker = PaperBroker(
            cfg.broker.paper_state,
            cfg.broker.paper_starting_cash,
            prices,
            cfg.execution.slippage_bps,
            cfg.execution.commission,
        )

    dry_run = not args.execute
    report = run_cycle(cfg, broker, bars, dry_run=dry_run)

    mode = f"{cfg.broker.mode.upper()}{' (dry run)' if dry_run else ''}"
    acct = report.account
    print(f"[{mode}] equity ${acct.equity:,.2f}  cash ${acct.cash:,.2f}")
    for sym in cfg.symbols:
        if sym in report.signals:
            state = "LONG" if report.signals[sym] else "FLAT"
            print(
                f"  {sym:6} signal {state}  price {report.prices[sym]:,.2f}  "
                f"held {acct.positions.get(sym, 0):g}"
            )
        else:
            print(f"  {sym:6} skipped: {report.skipped.get(sym, 'unknown')}")
    if report.risk.halted:
        print(f"HALTED: {report.risk.halted}")
    if not report.planned:
        print("No orders needed.")
    for o in report.risk.approved:
        limit = f"limit {o.limit_price:,.2f}" if o.limit_price else "market"
        print(f"  ORDER {o.side:4} {o.quantity:g} {o.symbol} {limit}  ({o.reason})")
    for o, reason in report.risk.rejected:
        print(f"  REJECTED {o.side} {o.quantity:g} {o.symbol}: {reason}")
    for r in report.results:
        print(f"  -> {r['order']['symbol']} {r['order']['side']}: {r['status']}"
              + (f" {r.get('reason') or r.get('error') or ''}" if r["status"] not in ("filled",) else ""))
    if dry_run and report.risk.approved:
        print("Dry run: nothing was submitted. Re-run with --execute to place these orders.")
    return 0


def cmd_fetch_data(cfg: Config, args: argparse.Namespace) -> int:
    from pathlib import Path

    from .robinhood_client import login

    rh = login(Credentials.from_env())
    out = Path(cfg.data.csv_dir)
    out.mkdir(parents=True, exist_ok=True)
    for sym in cfg.symbols:
        df = fetch_robinhood(rh, sym, span=args.span)
        df.to_csv(out / f"{sym}.csv", index_label="date", date_format="%Y-%m-%d")
        print(f"{sym}: {len(df)} bars, {df.index[0].date()} -> {df.index[-1].date()}")
    return 0


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    bars = _load_bars(cfg)
    prices = {s: float(df["close"].iloc[-1]) for s, df in bars.items()}
    broker = PaperBroker(
        cfg.broker.paper_state, cfg.broker.paper_starting_cash, prices
    )
    acct = broker.account()
    start = cfg.broker.paper_starting_cash
    print(f"Paper account ({cfg.broker.paper_state}), valued at last close")
    print(f"  equity ${acct.equity:,.2f} ({(acct.equity / start - 1) * 100:+.2f}% vs ${start:,.0f})")
    print(f"  cash   ${acct.cash:,.2f}")
    for sym, qty in acct.positions.items():
        print(f"  {sym:6} {qty:g} @ {prices.get(sym, float('nan')):,.2f}")
    print(f"  fills: {len(broker.state['fills'])}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rhtrader", description="SMA-crossover trading bot for Robinhood stocks & ETFs."
    )
    parser.add_argument("--config", "-c", help="TOML config file (default: built-in defaults)")
    sub = parser.add_subparsers(dest="command", required=True)

    bt = sub.add_parser("backtest", help="simulate the strategy on historical data")
    bt.add_argument("--fast", type=int, help="override strategy.fast")
    bt.add_argument("--slow", type=int, help="override strategy.slow")
    bt.add_argument("--start", help="ignore data before this date (YYYY-MM-DD)")
    bt.add_argument("--trades", action="store_true", help="list every trade")
    bt.add_argument("--equity-csv", help="write daily equity curves to this CSV")

    tr = sub.add_parser("trade", help="run one trading cycle (dry run by default)")
    tr.add_argument("--execute", action="store_true", help="submit orders to the broker")
    tr.add_argument("--live", action="store_true", help="confirm real-money trading")

    sub.add_parser("status", help="show the paper trading account")

    fd = sub.add_parser("fetch-data", help="download daily bars from Robinhood to CSV")
    fd.add_argument("--span", default="5year", help="day, week, month, 3month, year or 5year")

    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    handler = {
        "backtest": cmd_backtest,
        "trade": cmd_trade,
        "status": cmd_status,
        "fetch-data": cmd_fetch_data,
    }
    return handler[args.command](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
