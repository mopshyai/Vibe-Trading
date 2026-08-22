from __future__ import annotations

from src.options_market.personal import evaluate_personal_candidate


def test_indicative_option_feed_cannot_be_trade_ready() -> None:
    candidate = {
        "symbol": "ABC",
        "contract_symbol": "ABC261016C00100000",
        "direction": "bullish",
        "option_type": "call",
        "ranking_score": 82.0,
        "bid": 2.40,
        "entry_ask": 2.50,
        "open_interest": 2400,
        "volume": 300,
        "implied_volatility": 0.42,
        "delta": 0.42,
        "theta": -0.04,
        "dte": 35,
        "max_loss_usd": 250.0,
        "data_source": "alpaca",
        "option_feed": "indicative",
        "execution_grade_feed": False,
    }
    ev = {
        "samples": 90,
        "calibration_valid": True,
        "positive_ev": True,
        "lower_confidence_bound_pct": 5.0,
        "empirical_target_hit_rate": 0.20,
    }
    result = evaluate_personal_candidate(
        candidate,
        account_equity_usd=50_000.0,
        ev_report=ev,
        walk_forward_report={"decision": "WALK_FORWARD_PASS"},
        regime_report={"regime": "risk_on_trend", "confidence": 0.9},
        realized_vol_pct=30.0,
        iv_percentile=55.0,
    )
    assert result["decision"] == "WATCH"
    assert "option_feed_not_execution_grade" in result["watch_reasons"]
