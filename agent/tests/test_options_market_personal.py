from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from src.options_market.personal import build_personal_shortlist, evaluate_personal_candidate
from src.options_market.regime import classify_market_regime
from src.options_market.volatility import assess_option_quality


def _bars(*, start: float, daily_return: float, volatility: float = 0.006, rows: int = 260) -> pd.DataFrame:
    index = pd.date_range("2025-01-02", periods=rows, freq="B", tz="UTC")
    steps = np.arange(rows, dtype=float)
    close = start * np.exp((daily_return * steps) + volatility * np.sin(steps / 5.0))
    return pd.DataFrame(
        {
            "open": close * 0.998,
            "high": close * 1.006,
            "low": close * 0.994,
            "close": close,
            "volume": np.full(rows, 10_000_000.0),
        },
        index=index,
    )


def _candidate() -> dict:
    return {
        "symbol": "ABC",
        "contract_symbol": "ABC261016C00100000",
        "direction": "bullish",
        "option_type": "call",
        "ranking_score": 82.0,
        "bid": 2.40,
        "entry_ask": 2.50,
        "open_interest": 2400,
        "volume": 350,
        "implied_volatility": 0.42,
        "delta": 0.42,
        "gamma": 0.03,
        "theta": -0.04,
        "vega": 0.12,
        "dte": 35,
        "max_loss_usd": 250.0,
        "target_profit_pct": 300.0,
        "target_multiple": 4.0,
        "catalyst_score": 75.0,
    }


def _ev(*, positive: bool = True) -> dict:
    return {
        "samples": 90,
        "calibration_valid": True,
        "positive_ev": positive,
        "expected_return_pct": 18.0 if positive else -12.0,
        "lower_confidence_bound_pct": 6.0 if positive else -22.0,
        "empirical_target_hit_rate": 0.21 if positive else 0.08,
    }


def _walk(*, passed: bool = True) -> dict:
    return {"decision": "WALK_FORWARD_PASS" if passed else "WALK_FORWARD_INSUFFICIENT_OR_FAIL"}


def test_regime_identifies_broad_risk_on_trend() -> None:
    report = classify_market_regime(
        {
            "SPY": _bars(start=500.0, daily_return=0.0018),
            "QQQ": _bars(start=420.0, daily_return=0.0021),
            "IWM": _bars(start=200.0, daily_return=0.0014),
        },
        bullish_breadth_pct=70.0,
    )
    assert report["regime"] == "risk_on_trend"
    assert report["trend_up_score"] > report["trend_down_score"]


def test_option_quality_rejects_wide_spread() -> None:
    candidate = _candidate()
    candidate["bid"] = 1.50
    candidate["entry_ask"] = 2.50
    report = assess_option_quality(candidate, catalyst_score=80.0, realized_vol_pct=28.0, iv_percentile=55.0)
    assert report["passed"] is False
    assert "spread_too_wide" in report["hard_reasons"]


def test_personal_candidate_is_watch_when_calibration_missing() -> None:
    report = evaluate_personal_candidate(
        _candidate(),
        account_equity_usd=50_000.0,
        regime_report={"regime": "risk_on_trend", "confidence": 0.85},
    )
    assert report["decision"] == "WATCH"
    assert "empirical_ev_not_available" in report["watch_reasons"]
    assert "walk_forward_not_available" in report["watch_reasons"]


def test_personal_candidate_passes_negative_empirical_ev() -> None:
    report = evaluate_personal_candidate(
        _candidate(),
        account_equity_usd=50_000.0,
        ev_report=_ev(positive=False),
        walk_forward_report=_walk(passed=True),
        regime_report={"regime": "risk_on_trend", "confidence": 0.85},
    )
    assert report["decision"] == "PASS"
    assert "empirical_ev_not_positive" in report["hard_reasons"]


def test_personal_candidate_trade_ready_requires_all_gates() -> None:
    report = evaluate_personal_candidate(
        _candidate(),
        account_equity_usd=50_000.0,
        ev_report=_ev(positive=True),
        walk_forward_report=_walk(passed=True),
        regime_report={"regime": "risk_on_trend", "confidence": 0.90},
        realized_vol_pct=30.0,
        iv_percentile=60.0,
    )
    assert report["decision"] == "TRADE_READY_RESEARCH"
    assert report["risk"]["approved"] is True
    assert report["max_contracts_within_configured_single_trade_risk"] == 1


def test_personal_shortlist_caps_trade_ready_candidates() -> None:
    candidates = []
    ev_reports = {}
    walk_reports = {}
    for index in range(4):
        row = _candidate()
        row["symbol"] = f"A{index}"
        row["contract_symbol"] = f"A{index}261016C00100000"
        row["ranking_score"] = 90.0 - index
        candidates.append(row)
        ev_reports[row["contract_symbol"]] = _ev(positive=True)
        walk_reports[row["contract_symbol"]] = _walk(passed=True)

    result = build_personal_shortlist(
        candidates,
        account_equity_usd=100_000.0,
        ev_reports=ev_reports,
        walk_forward_reports=walk_reports,
        regime_report={"regime": "risk_on_trend", "confidence": 0.9},
    )
    ready = [row for row in result["candidates"] if row["decision"] == "TRADE_READY_RESEARCH"]
    assert len(ready) <= 2
    assert result["decision"] == "TRADE_READY_RESEARCH"
