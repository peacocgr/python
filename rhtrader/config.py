"""Configuration loading.

Settings live in a TOML file (see ``config.example.toml``). No credentials
are stored there: Robinhood access uses OAuth (``rhtrader login``), and the
tokens are kept in ``robinhood.token_file``.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class StrategyConfig:
    fast: int = 50
    slow: int = 200


@dataclass
class PortfolioConfig:
    # Fraction of equity allocated to each symbol while its signal is on.
    # Defaults to an equal split across all symbols.
    max_position_pct: float | None = None
    # Fraction of equity always left in cash.
    cash_buffer_pct: float = 0.02
    # Robinhood only supports fractional shares on market orders, so the
    # default is whole shares, which lets us use protective limit orders.
    fractional: bool = False


@dataclass
class ExecutionConfig:
    slippage_bps: float = 5.0
    commission: float = 0.0
    # Limit orders are placed this far through the last price.
    limit_buffer_bps: float = 20.0


@dataclass
class RiskConfig:
    max_order_notional: float = 5_000.0
    max_orders_per_run: int = 10
    # Refuse to trade a symbol whose latest completed bar is older than this.
    max_data_age_days: int = 5
    # Don't open a position when the live price is this far from the last
    # close (bad quote or a gap worth a human look). Exits are never blocked.
    max_price_gap_pct: float = 0.10
    # If this file exists, the trader refuses to place any orders.
    kill_switch_file: str = "KILL"


@dataclass
class BrokerConfig:
    mode: str = "paper"  # "paper" or "live"
    paper_state: str = "state/paper.json"
    paper_starting_cash: float = 10_000.0


@dataclass
class DataConfig:
    source: str = "csv"  # "csv" or "robinhood"
    csv_dir: str = "data"


@dataclass
class RobinhoodConfig:
    mcp_url: str = "https://agent.robinhood.com/mcp/trading"
    # The account you enabled for agentic trading. `rhtrader accounts` lists it.
    account_number: str = ""
    token_file: str = "~/.config/rhtrader/oauth.json"
    callback_port: int = 8765
    # Run Robinhood's pre-trade review first; skip any order it flags.
    review_orders: bool = True


@dataclass
class Config:
    symbols: list[str] = field(default_factory=lambda: ["SPY", "QQQ"])
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    portfolio: PortfolioConfig = field(default_factory=PortfolioConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    data: DataConfig = field(default_factory=DataConfig)
    robinhood: RobinhoodConfig = field(default_factory=RobinhoodConfig)
    trade_log: str = "state/trades.jsonl"

    @property
    def position_weight(self) -> float:
        equal = (1.0 - self.portfolio.cash_buffer_pct) / len(self.symbols)
        if self.portfolio.max_position_pct is None:
            return equal
        return min(equal, self.portfolio.max_position_pct)

    def validate(self) -> None:
        if not self.symbols:
            raise ValueError("config must list at least one symbol")
        if self.strategy.fast >= self.strategy.slow:
            raise ValueError("strategy.fast must be shorter than strategy.slow")
        if self.broker.mode not in ("paper", "live"):
            raise ValueError("broker.mode must be 'paper' or 'live'")
        if self.data.source not in ("csv", "robinhood"):
            raise ValueError("data.source must be 'csv' or 'robinhood'")
        if not 0 <= self.portfolio.cash_buffer_pct < 1:
            raise ValueError("portfolio.cash_buffer_pct must be in [0, 1)")
        if self.broker.mode == "live" and not self.robinhood.account_number:
            raise ValueError(
                "broker.mode = 'live' needs robinhood.account_number "
                "(run `rhtrader accounts` to find your agentic account)"
            )


_SECTIONS = {
    "strategy": StrategyConfig,
    "portfolio": PortfolioConfig,
    "execution": ExecutionConfig,
    "risk": RiskConfig,
    "broker": BrokerConfig,
    "data": DataConfig,
    "robinhood": RobinhoodConfig,
}


def load_config(path: str | Path | None) -> Config:
    raw: dict = {}
    if path is not None:
        with open(path, "rb") as f:
            raw = tomllib.load(f)

    kwargs: dict = {}
    for name, cls in _SECTIONS.items():
        section = raw.pop(name, {})
        unknown = set(section) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown keys in [{name}]: {sorted(unknown)}")
        kwargs[name] = cls(**section)
    unknown = set(raw) - set(Config.__dataclass_fields__)
    if unknown:
        raise ValueError(f"unknown top-level config keys: {sorted(unknown)}")
    kwargs.update(raw)
    if "symbols" in kwargs:
        kwargs["symbols"] = [s.upper() for s in kwargs["symbols"]]

    cfg = Config(**kwargs)
    cfg.validate()
    return cfg
