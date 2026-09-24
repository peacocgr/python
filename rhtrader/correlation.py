"""Track how two funds move relative to each other (e.g. SPMO vs SCHD).

Computed from the same saved Robinhood responses as ``rhtrader plan``:
``get_equity_historicals.json`` (about 3 years of daily bars for both
symbols) and, optionally, ``get_equity_quotes.json`` for today's official
close.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

from .data import NEW_YORK, align, append_official_closes, fetch_robinhood
from .snapshot import SnapshotClient

# Bands for the 52-week correlation of weekly returns.
BANDS = ((0.3, "diversifying"), (0.7, "moderate"), (float("inf"), "moving together"))

# Market stress from SPY's 1-month (21-day) annualized volatility. Thresholds
# are the 2016-2026 quartiles, rounded; "typical" is SPMO/SCHD's daily-return
# correlation on days in that state over the same period.
STRESS = (
    (0.09, "calm", 0.21),
    (0.13, "normal", 0.32),
    (0.18, "elevated", 0.44),
    (float("inf"), "stressed", 0.84),
)


def stress_state(vol: float) -> tuple[str, float]:
    for upper, label, typical in STRESS:
        if vol < upper:
            return label, typical
    return STRESS[-1][1], STRESS[-1][2]


def market_stress(market_close: pd.Series) -> dict:
    vol = (market_close.pct_change().rolling(21).std() * 252**0.5).dropna()
    if len(vol) < 6:
        raise ValueError("need at least ~27 days of market history")
    now, week_ago = float(vol.iloc[-1]), float(vol.iloc[-6])
    state, typical = stress_state(now)
    return {
        "volatility_1m": round(now * 100, 1),
        "volatility_1m_week_ago": round(week_ago * 100, 1),
        "state": state,
        "state_week_ago": stress_state(week_ago)[0],
        "typical_corr_in_state": typical,
    }


def band(corr: float) -> str:
    for upper, label in BANDS:
        if corr < upper:
            return label
    return BANDS[-1][1]


def correlation_report(closes: pd.DataFrame, a: str, b: str) -> dict:
    daily = closes.pct_change().dropna()
    weekly = closes.resample("W-FRI").last().pct_change().dropna()
    monthly = closes.resample("ME").last().pct_change().dropna()

    corr_52w = weekly[a].rolling(52).corr(weekly[b]).dropna()
    if corr_52w.empty:
        raise ValueError("need at least ~13 months of history for a 52-week correlation")
    corr_63d = daily[a].rolling(63).corr(daily[b]).dropna()

    def weeks_ago(n: int) -> float | None:
        return round(float(corr_52w.iloc[-1 - n]), 2) if len(corr_52w) > n else None

    now = float(corr_52w.iloc[-1])
    month_ago = weeks_ago(4)
    last_day = closes.index[-1]
    months = []
    for day, row in monthly.tail(13).iterrows():
        partial = day.to_period("M") == last_day.to_period("M")
        months.append({
            "month": day.strftime("%Y-%m") + (" (to date)" if partial else ""),
            a: round(float(row[a]) * 100, 1),
            b: round(float(row[b]) * 100, 1),
            "opposite": bool((row[a] > 0) != (row[b] > 0)),
        })
    full = [m for m in months if "to date" not in m["month"]][-12:]

    return {
        "pair": [a, b],
        "as_of": str(last_day.date()),
        "corr_52w": round(now, 2),
        "band": band(now),
        "band_changed": month_ago is not None and band(month_ago) != band(now),
        "corr_52w_history": {
            "4 weeks ago": month_ago,
            "3 months ago": weeks_ago(13),
            "1 year ago": weeks_ago(52),
            "2 years ago": weeks_ago(104),
        },
        "corr_63d": round(float(corr_63d.iloc[-1]), 2) if len(corr_63d) else None,
        "corr_full_period_weekly": round(float(weekly[a].corr(weekly[b])), 2),
        "history_start": str(closes.index[0].date()),
        "opposite_months_last_12": sum(m["opposite"] for m in full),
        "months": months,
        "week": {
            s: round(float(closes[s].iloc[-1] / closes[s].iloc[-6] - 1) * 100, 1) for s in (a, b)
        } if len(closes) > 5 else None,
    }


def report_from_snapshot(
    directory: str | Path, a: str = "SPMO", b: str = "SCHD", market: str = "SPY"
) -> dict:
    client = SnapshotClient(directory)
    try:
        bars = fetch_robinhood(client, [a, b, market], datetime.now(NEW_YORK))
    except RuntimeError:  # market index not in the snapshot: report without stress
        bars = fetch_robinhood(client, [a, b], datetime.now(NEW_YORK))
    if (Path(directory) / "get_equity_quotes.json").exists():
        bars = append_official_closes(client, bars)
    bars = align(bars)
    closes = pd.DataFrame({s: df["close"] for s, df in bars.items()})
    report = correlation_report(closes[[a, b]], a, b)
    if market in closes:
        report["market"] = {"symbol": market, **market_stress(closes[market])}
    return report


def format_report(r: dict) -> str:
    a, b = r["pair"]
    h = r["corr_52w_history"]
    fmt = lambda v: "n/a" if v is None else f"{v:+.2f}"  # noqa: E731
    lines = [
        f"{a} vs {b} correlation, as of {r['as_of']}",
        f"  1-year (weekly returns): {r['corr_52w']:+.2f}  -> {r['band'].upper()}"
        + ("   ** BAND CHANGED THIS MONTH **" if r["band_changed"] else ""),
        f"  trend: 4 wks ago {fmt(h['4 weeks ago'])}, 3 mo ago {fmt(h['3 months ago'])}, "
        f"1 yr ago {fmt(h['1 year ago'])}, 2 yrs ago {fmt(h['2 years ago'])}",
        f"  3-month (daily returns): {fmt(r['corr_63d'])}",
        f"  whole period since {r['history_start']} (weekly): {r['corr_full_period_weekly']:+.2f}",
        f"  opposite-direction months, last 12: {r['opposite_months_last_12']}",
    ]
    if r["week"]:
        lines.append(f"  last 5 trading days: {a} {r['week'][a]:+.1f}%, {b} {r['week'][b]:+.1f}%")
    mk = r.get("market")
    if mk:
        changed = mk["state"] != mk["state_week_ago"]
        lines.append(
            f"  market stress ({mk['symbol']} 1-month volatility): {mk['volatility_1m']:.1f}% "
            f"-> {mk['state'].upper()}"
            + (f"   ** CHANGED from {mk['state_week_ago']} a week ago **" if changed else "")
        )
        lines.append(
            f"    in {mk['state']} markets {a}/{b} daily correlation has typically been "
            f"{mk['typical_corr_in_state']:+.2f} (2016-2026)"
        )
    lines.append(f"\n  {'month':18}{a:>8}{b:>8}")
    for m in r["months"]:
        flag = "  opposite" if m["opposite"] else ""
        lines.append(f"  {m['month']:18}{m[a]:+7.1f}%{m[b]:+7.1f}%{flag}")
    lines.append("\n  Correlation bands: below 0.30 diversifying, 0.30-0.70 moderate, above 0.70 moving together.")
    if mk:
        lines.append(
            "  Stress: calm <9%, normal 9-13%, elevated 13-18%, stressed >18% volatility. "
            "In stressed markets the two usually fall together."
        )
    return "\n".join(lines)
