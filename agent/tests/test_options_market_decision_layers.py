from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from src.options_market.catalyst import CatalystEvent, directional_catalyst_scores, score_catalysts
from src.options_market.ev import ExpectedValueConfig, choose_score_threshold, empirical_expected_value
from src.options_market.paper_execution import PaperExecutionConfig, build_paper_option_order, submit_paper_option_order
from src.options_market.risk import PortfolioRiskConfig, assess_portfolio_risk
from src.trading.connectors.alpaca.sdk import AlpacaConfig


def _passing_reports() -> tuple[dict, dict, dict]:
    ev = {"positive_ev": True}
    walk = {"decision": "WALK_FORWARD_PASS"}
    risk = {"approved": True}
    return ev, walk, risk


def test_catalyst_scoring_is_point_in_time_and_direction_aligned() -> None:
    now = datetime(2026, 8, 22, 16, 0, tzinfo=timezone.utc)
    events = [
        CatalystEvent(
            symbol="TSLA.US",
            event_type="regulatory",
            published_at=now - timedelta(hours=20),
            direction="bullish",
            magnitude=0.95,
            source_quality=0.95,
            novelty=0.9,
            source="state-regulator",
        ),
        CatalystEvent(
            symbol="TSLA.US",
            event_type="news",
            published_at=now + timedelta(hours=1),
            direction="bearish",
            magnitude=1.0,
            source_quality=1.0,
            novelty=1.0,
            source="future-wire",
        ),
    ]
    scored = score_catalysts(events, as_of=now)
    assert scored["TSLA.US"]["direction"] == "bullish"
    assert scored["TSLA.US"]["event_count"] == 1

    aligned = directional_catalyst_scores(
        scored,
        [
            {"symbol": "TSLA.US", "direction": "bullish"},
            {"symbol": "TSLA.US", "direction": "bearish"},
        ],
    )
    assert aligned["TSLA.US"] > 0


def test_empirical_ev_requires_samples_and_positive_lower_bound() -> None:
    outcomes = []
    for i in range(100):
        outcomes.append(
            {
                "target_hit": i < 30,
                "end_return_pct": -50.0,
                "ranking_score": 85.0,
            }
        )
    report = empirical_expected_value(outcomes, config=ExpectedValueConfig(min_samples=50))
    assert report["samples"] == 100
    assert report["expected_return_pct"] == 55.0
    assert report["lower_confidence_bound_pct"] > 0
    assert report["positive_ev"] is True


def test_score_threshold_is_selected_from_training_outcomes_only() -> None:
    outcomes = []
    for i in range(200):
        high = i % 4 == 0
        outcomes.append(
            {
                "target_hit": high,
                "end_return_pct": -60.0,
                "ranking_score": 90.0 if high else 60.0,
            }
        )
    result = choose_score_threshold(
        outcomes,
        config=ExpectedValueConfig(min_samples=20, min_lower_confidence_bound_pct=0),
    )
    assert result["decision"] == "THRESHOLD_SELECTED"
    assert result["threshold"] >= 65.0


def test_portfolio_risk_gate_requires_ev_and_walk_forward() -> None:
    candidate = {
        "symbol": "TSLA",
        "underlying": "TSLA",
        "option_type": "call",
        "side": "buy",
        "entry_ask": 2.72,
        "max_loss_usd": 272.0,
        "expiration": "2026-09-18",
        "theme": "autonomy",
    }
    rejected = assess_portfolio_risk(
        candidate,
        account_equity_usd=50_000,
        config=PortfolioRiskConfig(),
    )
    assert rejected["approved"] is False
    assert "positive_ev_required" in rejected["reasons"]

    approved = assess_portfolio_risk(
        candidate,
        account_equity_usd=50_000,
        ev_report={"positive_ev": True},
        walk_forward_report={"decision": "WALK_FORWARD_PASS"},
    )
    assert approved["approved"] is True
    assert approved["proposed_max_loss_usd"] == 272.0


def test_tsla_example_builds_one_contract_four_x_paper_plan() -> None:
    ev, walk, risk = _passing_reports()
    candidate = {
        "contract_symbol": "TSLA260918C00400000",
        "option_type": "call",
        "bid": 2.69,
        "entry_ask": 2.72,
        "target_profit_pct": 300.0,
    }
    result = build_paper_option_order(
        candidate,
        quantity=1,
        ev_report=ev,
        walk_forward_report=walk,
        risk_report=risk,
    )
    assert result["ready"] is True
    assert result["order"]["symbol"] == "TSLA260918C00400000"
    assert result["order"]["quantity"] == 1
    assert result["max_premium_risk_usd"] == 272.0
    assert result["target_multiple"] == 4.0
    assert result["target_premium"] == 10.88


def test_paper_executor_refuses_live_alpaca_profile_before_order_call() -> None:
    ev, walk, risk = _passing_reports()
    candidate = {
        "contract_symbol": "TSLA260918C00400000",
        "option_type": "call",
        "bid": 2.69,
        "entry_ask": 2.72,
        "target_profit_pct": 300.0,
    }
    live = AlpacaConfig(api_key="x", secret_key="y", profile="live")
    with patch("src.options_market.paper_execution.alpaca_sdk.place_order") as place:
        result = submit_paper_option_order(
            candidate,
            quantity=1,
            market_is_open=True,
            ev_report=ev,
            walk_forward_report=walk,
            risk_report=risk,
            alpaca_config=live,
            config=PaperExecutionConfig(dry_run=False),
        )
    assert result["decision"] == "PAPER_ORDER_REJECTED"
    assert "alpaca_paper_profile_required" in result["reasons"]
    place.assert_not_called()


def test_paper_executor_does_not_queue_over_weekend_by_default() -> None:
    ev, walk, risk = _passing_reports()
    candidate = {
        "contract_symbol": "TSLA260918C00400000",
        "option_type": "call",
        "bid": 2.69,
        "entry_ask": 2.72,
    }
    paper = AlpacaConfig(api_key="x", secret_key="y", profile="paper")
    with patch("src.options_market.paper_execution.alpaca_sdk.place_order") as place:
        result = submit_paper_option_order(
            candidate,
            quantity=1,
            market_is_open=False,
            ev_report=ev,
            walk_forward_report=walk,
            risk_report=risk,
            alpaca_config=paper,
            config=PaperExecutionConfig(dry_run=False),
        )
    assert result["decision"] == "PAPER_ORDER_REJECTED"
    assert "market_open_confirmation_required" in result["reasons"]
    place.assert_not_called()
