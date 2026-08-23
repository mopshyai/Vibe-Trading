from __future__ import annotations

from src.trading_platform import PlatformEnvironment, TradingPlatformService, TradingPlatformStore


def test_surface_evidence_survives_snapshot_and_candidate_journal(tmp_path) -> None:
    cycle = {
        "market": {"state": {"cycle": 7}, "plan": {"phase": "regular"}},
        "personal": {
            "decision": "WATCH",
            "journal_candidates": [
                {
                    "symbol": "XYZ",
                    "contract_symbol": "XYZ260918C00110000",
                    "decision": "WATCH",
                    "displayed": True,
                    "surface_efficiency_score": 44.0,
                    "surface_required_move_ratio": 1.45,
                    "surface_iv_percentile": 88.0,
                    "candidate_iv_premium_to_atm_points": 12.0,
                    "surface_atm_expected_move_pct": 15.2,
                    "surface_term_structure_state": "front_loaded",
                    "surface_skew_state": "put_skew",
                    "surface_implied_vs_realized_state": "rich",
                }
            ],
        },
        "dashboard": {
            "decision": "WATCH",
            "headline": "surface-aware watch",
            "market_regime": "risk_on_trend",
            "funnel": {"trade_ready": 0},
            "cards": [
                {
                    "symbol": "XYZ",
                    "contract_symbol": "XYZ260918C00110000",
                    "decision": "WATCH",
                    "surface_efficiency_score": 44.0,
                    "surface_required_move_ratio": 1.45,
                    "candidate_iv_premium_to_atm_points": 12.0,
                    "surface_atm_expected_move_pct": 15.2,
                    "surface_term_structure_state": "front_loaded",
                    "surface_skew_state": "put_skew",
                    "surface_implied_vs_realized_state": "rich",
                }
            ],
        },
    }

    with TradingPlatformStore(tmp_path / "platform.duckdb") as store:
        service = TradingPlatformService(store=store, environment=PlatformEnvironment.RESEARCH)
        snapshot = service.publish_personal_cycle(cycle)
        card = snapshot.opportunities[0]
        assert card.surface_efficiency_score == 44.0
        assert card.surface_required_move_ratio == 1.45
        assert card.surface_term_structure_state == "front_loaded"
        assert card.surface_skew_state == "put_skew"

        journal = store.recent_journal(limit=10)
        assert journal[0]["surface_efficiency_score"] == 44.0
        assert journal[0]["surface_iv_percentile"] == 88.0
        assert journal[0]["surface_implied_vs_realized_state"] == "rich"
