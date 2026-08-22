from __future__ import annotations

from datetime import datetime, timezone

from src.options_market.personal_service import (
    PersonalAccountState,
    PersonalContinuousOptionsService,
    PersonalEvidence,
)


class _Analyzer:
    def run_cycle(self, **_: object) -> dict:
        return {
            "chart_stage": {"universe_received": 5500, "eligible_for_cross_section": 1800, "candidate_count": 200},
            "final_stage": {
                "candidate_count": 1,
                "candidates": [
                    {
                        "symbol": "ABC",
                        "contract_symbol": "ABC261016C00100000",
                        "direction": "bullish",
                        "option_type": "call",
                        "ranking_score": 82.0,
                        "bid": 2.40,
                        "entry_ask": 2.50,
                        "open_interest": 2000,
                        "volume": 300,
                        "implied_volatility": 0.40,
                        "delta": 0.42,
                        "theta": -0.04,
                        "dte": 35,
                        "max_loss_usd": 250.0,
                    }
                ],
            },
            "state": {"latest_shortlist": []},
        }


def test_personal_service_composes_market_evidence_and_alert() -> None:
    candidate_key = "ABC261016C00100000"

    def account_provider(_: datetime) -> PersonalAccountState:
        return PersonalAccountState(equity_usd=50_000.0)

    def evidence_provider(candidates, _: datetime) -> PersonalEvidence:
        assert candidates[0]["contract_symbol"] == candidate_key
        return PersonalEvidence(
            ev_reports={
                candidate_key: {
                    "samples": 90,
                    "calibration_valid": True,
                    "positive_ev": True,
                    "expected_return_pct": 18.0,
                    "lower_confidence_bound_pct": 6.0,
                    "empirical_target_hit_rate": 0.20,
                }
            },
            walk_forward_reports={candidate_key: {"decision": "WALK_FORWARD_PASS"}},
            regime_report={"regime": "risk_on_trend", "confidence": 0.9},
            realized_vol_by_symbol={"ABC": 28.0},
            iv_percentile_by_contract={candidate_key: 55.0},
        )

    service = PersonalContinuousOptionsService(
        analyzer=_Analyzer(),  # type: ignore[arg-type]
        account_provider=account_provider,
        evidence_provider=evidence_provider,
    )
    result = service.run_cycle(
        now=datetime(2026, 8, 24, 15, 0, tzinfo=timezone.utc),
        session=None,  # type: ignore[arg-type]
    )
    assert result["personal"]["decision"] == "TRADE_READY_RESEARCH"
    assert result["dashboard"]["funnel"]["universe"] == 5500
    assert result["alert"]["should_alert"] is True
    assert result["execution"] == "none"
