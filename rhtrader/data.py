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


def fetch_robinhood(
    client, symbols: list[str], start: datetime
) -> dict[str, pd.DataFrame]:
    """Fetch split-adjusted daily bars through Robinhood's MCP server.

    ``client`` is a connected :class:`rhtrader.robinhood_mcp.RobinhoodMCP`.
    """
    start_time = start.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
    out: dict[str, pd.DataFrame] = {}
    for i in range(0, len(symbols), 10):  # the tool accepts 10 symbols per call
        data = client.call(
            "get_equity_historicals",
            {"symbols": symbols[i : i + 10], "start_time": start_time, "interval": "day"},
        )
        for result in data["results"]:
            out[result["symbol"]] = _bars_to_frame(result["bars"])
    missing = set(symbols) - set(out)
    if missing:
        raise RuntimeError(f"Robinhood returned no bars for {sorted(missing)}")
    return out


def _bars_to_frame(bars: list[dict]) -> pd.DataFrame:
    bars = [b for b in bars if not b.get("interpolated")]
    df = pd.DataFrame(
        {
            "date": pd.to_datetime([b["begins_at"][:10] for b in bars]),
            "open": [b["open_price"] for b in bars],
            "high": [b["high_price"] for b in bars],
            "low": [b["low_price"] for b in bars],
            "close": [b["close_price"] for b in bars],
            "volume": [b["volume"] for b in bars],
        }
    ).set_index("date")
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
