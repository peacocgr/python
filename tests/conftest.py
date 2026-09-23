import numpy as np
import pandas as pd
import pytest

from rhtrader.config import Config, StrategyConfig


def make_bars(closes, start="2024-01-01") -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    idx = pd.bdate_range(start, periods=len(closes))
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes * 1.01,
            "low": closes * 0.99,
            "close": closes,
            "volume": 1_000_000.0,
        },
        index=idx,
    )


@pytest.fixture
def cfg(tmp_path) -> Config:
    c = Config(symbols=["AAA", "BBB"], strategy=StrategyConfig(fast=3, slow=5))
    c.execution.slippage_bps = 0.0
    c.portfolio.cash_buffer_pct = 0.0
    c.risk.kill_switch_file = str(tmp_path / "KILL")
    c.broker.paper_state = str(tmp_path / "paper.json")
    c.trade_log = str(tmp_path / "trades.jsonl")
    return c
