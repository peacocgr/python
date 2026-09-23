"""Daily OHLCV bar loading.

Every loader returns a DataFrame indexed by date (``DatetimeIndex``) with
float columns ``open, high, low, close, volume``, sorted ascending.
"""

from __future__ import annotations

from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

COLUMNS = ["open", "high", "low", "close", "volume"]
NEW_YORK = ZoneInfo("America/New_York")
MARKET_CLOSE = time(16, 0)


def load_csv(csv_dir: str | Path, symbol: str) -> pd.DataFrame:
    path = Path(csv_dir) / f"{symbol}.csv"
    df = pd.read_csv(path, parse_dates=["date"], index_col="date")
    missing = set(COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"{path} is missing columns {sorted(missing)}")
    return df[COLUMNS].astype(float).sort_index()


def fetch_robinhood(rh, symbol: str, span: str = "5year") -> pd.DataFrame:
    """Fetch daily bars via a logged-in ``robin_stocks.robinhood`` module."""
    rows = rh.stocks.get_stock_historicals(
        symbol, interval="day", span=span, bounds="regular"
    )
    if not rows:
        raise RuntimeError(f"Robinhood returned no historical data for {symbol}")
    df = pd.DataFrame(
        {
            "date": pd.to_datetime([r["begins_at"][:10] for r in rows]),
            "open": [r["open_price"] for r in rows],
            "high": [r["high_price"] for r in rows],
            "low": [r["low_price"] for r in rows],
            "close": [r["close_price"] for r in rows],
            "volume": [r["volume"] for r in rows],
        }
    ).set_index("date")
    if "interpolated" in rows[0]:
        df = df[[not r.get("interpolated") for r in rows]]
    return df[COLUMNS].astype(float).sort_index()


def drop_incomplete_bar(df: pd.DataFrame, now: datetime | None = None) -> pd.DataFrame:
    """Drop today's bar if the regular session hasn't closed yet.

    Signals must only use completed daily bars; otherwise an intraday
    price could flip a crossover that reverses by the close.
    """
    now = (now or datetime.now(NEW_YORK)).astimezone(NEW_YORK)
    if df.empty:
        return df
    last = df.index[-1].date()
    if last > now.date() or (last == now.date() and now.time() < MARKET_CLOSE):
        return df.iloc[:-1]
    return df


def align(bars: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Restrict every symbol to the dates all symbols share."""
    common = None
    for df in bars.values():
        common = df.index if common is None else common.intersection(df.index)
    return {s: df.loc[common] for s, df in bars.items()}
