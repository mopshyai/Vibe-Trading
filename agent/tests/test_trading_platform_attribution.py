from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from src.options_market import OptionsResearchStore
from src.trading_platform import (
    AttributionConfig,
    DeskDecision,
    JournalEntry,
    JournalStage,
    PlatformEnvironment,
    SystemIdentity,
    TradingPlatformStore,
    build_attribution_report,
    checkpoint_candidate_outcomes,
)

UTC = timezone.utc
CONTRACT = "AAPL260220C00250000"
DECISION_AT = datetime(2026, 1, 1, 15, 0, tzinfo=UTC)


def _source_entry(*, decision=DeskDecision.PASS, stage=JournalStage.REJECTED, reasons=None):
    return JournalEntry(
        occurred_at=DECISION_AT,
        stage=stage,
        environment=PlatformEnvironment.RESEARCH,
        system=SystemIdentity(),
        snapshot_id="snapshot-1",
        source_cycle=1,
        symbol="AAPL",
        contract_symbol=CONTRACT,
        decision=decision,
        direction="bullish",
        option_type="call",
        ranking_score=80.0,
        surface_efficiency_score=64.0,
        surface_required_move_ratio=1.25,
        surface_iv_percentile=72.0,
        candidate_iv_premium_to_atm_points=8.0,
        surface_atm_expected_move_pct=14.0,
        surface_term_structure_state="front_loaded",
        surface_skew_state="put_skew",
        surface_implied_vs_realized_state="rich",
        entry_ask=1.0,
        hard_reasons=list(reasons or ["negative_ev"]),
    )


def _quotes(bids: list[float]) -> pd.DataFrame:
    timestamps = pd.to_datetime(
        ["2026-01-02T15:00:00Z", "2026-01-03T15:00:00Z", "2026-01-06T14:00:00Z"],
        utc=True,
    )
    return pd.DataFrame(
        {
            "contract_symbol": [CONTRACT] * 3,
            "underlying": ["AAPL"] * 3,
            "event_ts": timestamps,
            "expiration": pd.to_datetime(["2026-02-20T21:00:00Z"] * 3, utc=True),
            "strike": [250.0] * 3,
            "option_type": ["call"] * 3,
            "bid": bids,
            "ask": [max(bid + 0.05, 0.05) for bid in bids],
            "source": ["test"] * 3,
            "available_at": timestamps,
        }
    )


def test_immature_candidate_is_not_labeled(tmp_path) -> None:
    with TradingPlatformStore(tmp_path / "platform.duckdb") as platform, OptionsResearchStore(":memory:") as research:
        platform.append_journal_entry(_source_entry())
        research.ingest_option_quotes(_quotes([1.2, 2.0, 4.2]))

        report = checkpoint_candidate_outcomes(
            platform_store=platform,
            research_store=research,
            as_of=datetime(2026, 1, 3, 16, 0, tzinfo=UTC),
            config=AttributionConfig(evaluation_horizon_days=5),
        )

        assert report["labeled"] == 0
        assert report["immature"] == 1
        assert all(row["stage"] != "outcome_observed" for row in platform.recent_journal(limit=20))


def test_mature_candidate_uses_future_bid_path_and_is_idempotent(tmp_path) -> None:
    with TradingPlatformStore(tmp_path / "platform.duckdb") as platform, OptionsResearchStore(":memory:") as research:
        source = _source_entry()
        platform.append_journal_entry(source)
        research.ingest_option_quotes(_quotes([1.2, 2.0, 4.2]))
        as_of = datetime(2026, 1, 10, 16, 0, tzinfo=UTC)

        first = checkpoint_candidate_outcomes(
            platform_store=platform,
            research_store=research,
            as_of=as_of,
            config=AttributionConfig(evaluation_horizon_days=5),
        )
        second = checkpoint_candidate_outcomes(
            platform_store=platform,
            research_store=research,
            as_of=as_of,
            config=AttributionConfig(evaluation_horizon_days=5),
        )

        assert first["labeled"] == 1
        assert first["outcomes"][0]["target_hit"] is True
        assert first["outcomes"][0]["touch_4x"] is True
        assert second["labeled"] == 0

        outcomes = [
            row for row in platform.recent_journal(limit=20)
            if row["stage"] == "outcome_observed"
        ]
        assert len(outcomes) == 1
        assert outcomes[0]["metadata"]["source_journal_id"] == source.journal_id
        assert outcomes[0]["metadata"]["evaluation_only"] is True
        assert outcomes[0]["surface_efficiency_score"] == 64.0
        assert outcomes[0]["surface_required_move_ratio"] == 1.25
        assert outcomes[0]["surface_term_structure_state"] == "front_loaded"
        assert outcomes[0]["surface_skew_state"] == "put_skew"


def test_insufficient_future_quotes_are_not_fabricated(tmp_path) -> None:
    with TradingPlatformStore(tmp_path / "platform.duckdb") as platform, OptionsResearchStore(":memory:") as research:
        platform.append_journal_entry(_source_entry())
        research.ingest_option_quotes(_quotes([1.2, 2.0, 4.2]).iloc[:1])

        report = checkpoint_candidate_outcomes(
            platform_store=platform,
            research_store=research,
            as_of=datetime(2026, 1, 10, 16, 0, tzinfo=UTC),
            config=AttributionConfig(evaluation_horizon_days=5, minimum_quote_observations=2),
        )

        assert report["labeled"] == 0
        assert report["skipped"][0]["reason"] == "insufficient_future_quotes"


def test_attribution_keeps_rejected_winners_and_avoided_losses() -> None:
    rows = [
        {
            "stage": "outcome_observed",
            "hard_reasons": ["negative_ev"],
            "watch_reasons": [],
            "surface_efficiency_score": 70.0,
            "surface_required_move_ratio": 0.9,
            "surface_term_structure_state": "front_loaded",
            "surface_skew_state": "put_skew",
            "surface_implied_vs_realized_state": "rich",
            "metadata": {"source_stage": "rejected"},
            "outcome": {
                "target_hit": True,
                "touch_2x": True,
                "full_loss_proxy": False,
                "end_return_pct": 120.0,
                "max_multiple": 4.2,
                "mfe_pct": 320.0,
                "mae_pct": -20.0,
            },
        },
        {
            "stage": "outcome_observed",
            "hard_reasons": ["negative_ev"],
            "watch_reasons": [],
            "surface_efficiency_score": 20.0,
            "surface_required_move_ratio": 1.8,
            "surface_term_structure_state": "back_loaded",
            "surface_skew_state": "balanced",
            "surface_implied_vs_realized_state": "cheap",
            "metadata": {"source_stage": "rejected"},
            "outcome": {
                "target_hit": False,
                "touch_2x": False,
                "full_loss_proxy": True,
                "end_return_pct": -98.0,
                "max_multiple": 1.05,
                "mfe_pct": 5.0,
                "mae_pct": -99.0,
            },
        },
    ]

    report = build_attribution_report(rows)
    assert report["samples"] == 2
    assert report["missed_opportunities"] == 1
    assert report["avoided_losses"] == 1
    assert report["reason_attribution"][0]["reason"] == "negative_ev"
    assert report["reason_attribution"][0]["samples"] == 2

    efficiency = {row["bucket"]: row for row in report["surface_attribution"]["efficiency_bucket"]}
    assert efficiency["strong_>=60"]["samples"] == 1
    assert efficiency["strong_>=60"]["target_hit_rate"] == 1.0
    assert efficiency["weak_<35"]["full_loss_proxy_rate"] == 1.0

    move = {row["bucket"]: row for row in report["surface_attribution"]["required_move_bucket"]}
    assert move["<1.0x"]["target_hit_rate"] == 1.0
    assert move["1.5_2.0x"]["target_hit_rate"] == 0.0

    term = {row["bucket"]: row for row in report["surface_attribution"]["term_structure"]}
    assert term["front_loaded"]["samples"] == 1
    assert term["back_loaded"]["samples"] == 1
