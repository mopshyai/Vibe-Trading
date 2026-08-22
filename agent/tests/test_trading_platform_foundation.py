from __future__ import annotations

from datetime import datetime, timezone

from src.trading_platform import (
    ComponentHealth,
    DataQualitySummary,
    DeskDecision,
    HealthStatus,
    PlatformEnvironment,
    RiskSummary,
    SystemIdentity,
    TradingPlatformService,
    TradingPlatformStore,
)


def _cycle() -> dict:
    return {
        "mode": "continuous_personal_options_research",
        "market": {
            "plan": {"phase": "regular"},
            "state": {"cycle": 12, "latest_phase": "regular"},
        },
        "personal": {"decision": "WATCH"},
        "dashboard": {
            "headline": "No trade-ready setup; strongest candidates remain on watch",
            "decision": "WATCH",
            "market_regime": "risk_on",
            "market_regime_confidence": 0.82,
            "funnel": {
                "universe": 5500,
                "chart_eligible": 1800,
                "chart_candidates": 200,
                "final_candidates": 8,
            },
            "cards": [
                {
                    "decision": "WATCH",
                    "symbol": "TSLA",
                    "contract_symbol": "TSLA260918C00400000",
                    "direction": "bullish",
                    "composite_score": 82.5,
                    "ranking_score": 83.1,
                    "option_quality_score": 78.0,
                    "regime_fit_score": 80.0,
                    "historical_samples": 44,
                    "entry_ask": 2.72,
                    "max_loss_usd_per_contract": 272.0,
                    "configured_contract_cap": 1,
                    "watch_reasons": ["empirical_ev_sample_insufficient"],
                }
            ],
            "disclaimer": "Research dashboard only; broker execution is separate.",
        },
        "alert": {"should_alert": True, "reasons": ["new_top_watch"]},
        "execution": "none",
    }


def test_data_quality_blocks_stale_execution_grade_feed() -> None:
    health = DataQualitySummary.from_components(
        [
            ComponentHealth(
                name="opra",
                status=HealthStatus.STALE,
                observed_at=datetime.now(timezone.utc),
                age_seconds=120.0,
                blocking=True,
            )
        ]
    )
    assert not health.healthy
    assert health.blocking_reasons == ["opra:stale"]


def test_service_publishes_research_snapshot_and_audit_event(tmp_path) -> None:
    path = tmp_path / "platform.duckdb"
    quality = DataQualitySummary.from_components(
        [ComponentHealth(name="equity", status=HealthStatus.OK, blocking=True)]
    )
    with TradingPlatformStore(path) as store:
        service = TradingPlatformService(
            store=store,
            environment=PlatformEnvironment.RESEARCH,
            system=SystemIdentity(commit_sha="abc123"),
        )
        snapshot = service.publish_personal_cycle(
            _cycle(),
            data_quality=quality,
            risk=RiskSummary(account_equity_usd=50_000, positions=0),
        )

        assert snapshot.decision is DeskDecision.WATCH
        assert snapshot.execution_mode.value == "research_only"
        assert snapshot.funnel.universe == 5500
        assert snapshot.funnel.deep_analyzed == 8
        assert snapshot.opportunities[0].contract_symbol == "TSLA260918C00400000"
        assert snapshot.data_quality.healthy

        restored = store.latest_snapshot()
        assert restored is not None
        assert restored.snapshot_id == snapshot.snapshot_id
        events = store.recent_events(10)
        assert events[0]["event_type"] in {"research_alert", "desk_snapshot_published"}
        assert {row["event_type"] for row in events} == {
            "desk_snapshot_published",
            "research_alert",
        }


def test_live_environment_remains_disabled_in_foundation(tmp_path) -> None:
    with TradingPlatformStore(tmp_path / "platform.duckdb") as store:
        service = TradingPlatformService(store=store, environment=PlatformEnvironment.LIVE)
        snapshot = service.publish_personal_cycle(_cycle())
        assert snapshot.execution_mode.value == "live_disabled"
