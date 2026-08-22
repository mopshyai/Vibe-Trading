from __future__ import annotations

from src.options_market.dashboard import build_alert_event, build_personal_dashboard
from src.options_market.payoff import PayoffPolicyConfig, evaluate_payoff_targets


def test_payoff_selector_does_not_force_largest_target() -> None:
    outcomes = []
    for index in range(100):
        if index < 55:
            max_multiple = 2.2
            ending = -20.0
        elif index < 70:
            max_multiple = 3.2
            ending = -30.0
        elif index < 76:
            max_multiple = 4.2
            ending = -35.0
        else:
            max_multiple = 1.2
            ending = -45.0
        outcomes.append({"max_multiple": max_multiple, "end_return_pct": ending})

    result = evaluate_payoff_targets(
        outcomes,
        config=PayoffPolicyConfig(
            target_profit_pcts=(100.0, 200.0, 300.0),
            min_samples=50,
            confidence_level=0.80,
        ),
    )
    assert result["decision"] == "PAYOFF_POLICY_SELECTED"
    assert result["selected_target_profit_pct"] in {100.0, 200.0}
    assert result["selected_target_profit_pct"] != 300.0


def test_payoff_selector_empty_input_is_json_safe() -> None:
    result = evaluate_payoff_targets([])
    assert result["decision"] == "NO_PAYOFF_POLICY"
    assert all(row["lower_confidence_bound_pct"] is None for row in result["evaluations"])


def test_dashboard_alerts_on_new_trade_ready_candidate() -> None:
    report = {
        "decision": "TRADE_READY_RESEARCH",
        "regime": {"regime": "risk_on_trend", "confidence": 0.8},
        "candidates": [
            {
                "decision": "TRADE_READY_RESEARCH",
                "symbol": "ABC",
                "contract_symbol": "ABC261016C00100000",
                "direction": "bullish",
                "composite_score": 82.0,
                "ranking_score": 84.0,
                "option_quality_score": 80.0,
                "regime_fit_score": 78.0,
                "empirical_ev": {"samples": 90, "empirical_target_hit_rate": 0.2, "expected_return_pct": 15.0},
                "option_quality": {"metrics": {"spread_pct": 2.0, "iv_percentile": 60.0}},
                "risk": {"approved": True},
            }
        ],
    }
    dashboard = build_personal_dashboard(report, funnel={"universe": 5500, "final_candidates": 1})
    alert = build_alert_event(dashboard, {"decision": "WATCH", "cards": []})
    assert alert["should_alert"] is True
    assert "new_trade_ready_contract" in alert["reasons"]
    assert dashboard["trade_ready_count"] == 1
