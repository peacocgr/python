import numpy as np
import pandas as pd
import pytest

from rhtrader.backtest import run_backtest, size_order
from rhtrader.metrics import max_drawdown
from rhtrader.strategy import SmaCrossover

from .conftest import make_bars


def test_sma_signal_turns_on_in_uptrend_and_off_in_downtrend():
    close = pd.Series([10, 10, 10, 10, 10, 11, 12, 13, 14, 13, 11, 9, 7, 5], dtype=float)
    sig = SmaCrossover(3, 5).signals(close)
    assert sig.iloc[:4].eq(0).all()  # warmup
    assert sig.iloc[7] == 1
    assert sig.iloc[-1] == 0


def test_signal_only_uses_past_data():
    rng = np.random.default_rng(0)
    close = pd.Series(100 + rng.normal(0, 1, 300).cumsum())
    s = SmaCrossover(5, 20)
    full = s.signals(close)
    for t in (50, 150, 299):
        assert s.signals(close.iloc[: t + 1]).iloc[-1] == full.iloc[t]


def test_fast_must_be_shorter_than_slow():
    with pytest.raises(ValueError):
        SmaCrossover(10, 5)


def test_size_order_rounds_down():
    assert size_order(1000, 300, fractional=False) == 3
    assert size_order(1000, 300, fractional=True) == pytest.approx(3.333333)
    assert size_order(1000, 300, fractional=True) * 300 <= 1000
    assert size_order(-5, 300, fractional=False) == 0


def test_backtest_fills_at_next_open(cfg):
    closes = [10] * 5 + [11, 12, 13, 14, 15, 16]
    bars = make_bars(closes)
    bars["open"] = bars["close"] + 0.5  # distinguishable from close
    cfg.symbols = ["AAA"]
    result = run_backtest({"AAA": bars}, cfg)

    first = result.trades[0]
    sig = SmaCrossover(3, 5).signals(bars["close"])
    signal_day = sig[sig == 1].index[0]
    next_day = bars.index[bars.index.get_loc(signal_day) + 1]
    assert first.side == "buy"
    assert first.date == next_day
    assert first.price == bars.loc[next_day, "open"]


def test_backtest_exits_downtrend_and_beats_holding(cfg):
    up = list(np.linspace(100, 150, 40))
    down = list(np.linspace(150, 60, 40))
    cfg.symbols = ["AAA"]
    result = run_backtest({"AAA": make_bars(up + down)}, cfg)
    sides = [t.side for t in result.trades]
    assert sides == ["buy", "sell"]
    assert result.equity.iloc[-1] > result.benchmark.iloc[-1]
    assert result.stats["max_drawdown"] > max_drawdown(result.benchmark)


def test_backtest_never_goes_negative_cash(cfg):
    rng = np.random.default_rng(1)
    bars = {
        s: make_bars(100 + rng.normal(0, 2, 400).cumsum().clip(-50))
        for s in cfg.symbols
    }
    result = run_backtest(bars, cfg)
    assert (result.equity > 0).all()
    assert result.stats["trades"] > 0


def test_backtest_needs_enough_data(cfg):
    with pytest.raises(ValueError):
        run_backtest({s: make_bars([1, 2, 3]) for s in cfg.symbols}, cfg)
