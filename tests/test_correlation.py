import numpy as np
import pandas as pd
import pytest

from rhtrader.correlation import band, correlation_report, format_report


def _closes(returns_a, returns_b):
    idx = pd.bdate_range("2023-01-02", periods=len(returns_a))
    return pd.DataFrame(
        {"A": 100 * np.cumprod(1 + returns_a), "B": 100 * np.cumprod(1 + returns_b)}, index=idx
    )


def test_bands():
    assert band(-0.4) == "diversifying" and band(0.29) == "diversifying"
    assert band(0.5) == "moderate"
    assert band(0.9) == "moving together"


def test_identical_and_opposite_series():
    rng = np.random.default_rng(0)
    r = rng.normal(0, 0.01, 600)
    same = correlation_report(_closes(r, r), "A", "B")
    assert same["corr_52w"] == pytest.approx(1.0) and same["band"] == "moving together"
    assert same["opposite_months_last_12"] == 0

    opp = correlation_report(_closes(r, -r), "A", "B")
    assert opp["corr_52w"] < -0.9 and opp["band"] == "diversifying"
    assert opp["opposite_months_last_12"] >= 10


def test_band_change_is_flagged():
    rng = np.random.default_rng(1)
    r = rng.normal(0, 0.01, 700)
    # Move together for most of the history, then diverge hard in the last months.
    b = np.concatenate([r[:560], -r[560:]])
    rep = correlation_report(_closes(r, b), "A", "B")
    assert rep["corr_52w_history"]["1 year ago"] > 0.9
    assert rep["corr_52w"] < rep["corr_52w_history"]["1 year ago"]
    assert "correlation" in format_report(rep)


def test_needs_enough_history():
    r = np.zeros(100)
    with pytest.raises(ValueError):
        correlation_report(_closes(r, r), "A", "B")


def test_stress_states():
    from rhtrader.correlation import stress_state

    assert stress_state(0.05)[0] == "calm"
    assert stress_state(0.10)[0] == "normal"
    assert stress_state(0.15)[0] == "elevated"
    assert stress_state(0.30) == ("stressed", 0.84)


def test_market_stress_detects_a_volatility_jump():
    from rhtrader.correlation import market_stress

    rng = np.random.default_rng(2)
    calm = rng.normal(0, 0.004, 80)      # ~6% annualized
    shock = rng.normal(0, 0.03, 25)      # ~48% annualized
    idx = pd.bdate_range("2024-01-01", periods=105)
    before = market_stress(pd.Series(100 * np.cumprod(1 + calm), index=idx[:80]))
    after = market_stress(pd.Series(100 * np.cumprod(1 + np.concatenate([calm, shock])), index=idx))
    assert before["state"] == "calm"
    assert after["state"] == "stressed"
