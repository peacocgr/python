"""Trend-following strategy: simple moving-average crossover.

Long while the fast SMA is above the slow SMA, flat otherwise. The signal
on day *t* uses closes up to and including day *t*, so it can only be
acted on from day *t + 1* onwards.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class SmaCrossover:
    fast: int = 50
    slow: int = 200

    def __post_init__(self) -> None:
        if not 0 < self.fast < self.slow:
            raise ValueError("need 0 < fast < slow")

    @property
    def warmup(self) -> int:
        """Bars needed before the first valid signal."""
        return self.slow

    def indicators(self, close: pd.Series) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "close": close,
                "sma_fast": close.rolling(self.fast).mean(),
                "sma_slow": close.rolling(self.slow).mean(),
            }
        )

    def signals(self, close: pd.Series) -> pd.Series:
        """1 = hold long, 0 = flat. Zero during the warmup period."""
        ind = self.indicators(close)
        on = (ind["sma_fast"] > ind["sma_slow"]) & ind["sma_slow"].notna()
        return on.astype(int)
