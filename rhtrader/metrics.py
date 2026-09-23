"""Performance statistics for daily equity curves."""

from __future__ import annotations

import math

import pandas as pd

TRADING_DAYS = 252


def max_drawdown(equity: pd.Series) -> float:
    """Largest peak-to-trough decline, as a negative fraction."""
    return float((equity / equity.cummax() - 1.0).min())


def summarize(equity: pd.Series) -> dict[str, float]:
    returns = equity.pct_change().dropna()
    years = len(returns) / TRADING_DAYS
    total = equity.iloc[-1] / equity.iloc[0] - 1.0
    vol = returns.std() * math.sqrt(TRADING_DAYS)
    return {
        "total_return": float(total),
        "cagr": float((1.0 + total) ** (1.0 / years) - 1.0) if years > 0 else 0.0,
        "volatility": float(vol),
        "sharpe": float(returns.mean() / returns.std() * math.sqrt(TRADING_DAYS))
        if returns.std() > 0
        else 0.0,
        "max_drawdown": max_drawdown(equity),
    }
