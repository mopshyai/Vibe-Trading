from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

import src.options_market.experiments as experiments
from src.options_market.experiments import (
    ExperimentLineage,
    HistoricalExperimentConfig,
    run_historical_research_experiment,
    summarize_historical_experiment,
)

UTC = timezone.utc
RESEARCH = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)
EVALUATION = datetime(2026, 2, 15, 15, 0, tzinfo=UTC)
CONTRACT = "AAPL260220C00110000"


class FakeStore:
    def option_quotes_asof(self, *, as_of, start=None, end=None, underlyings=None, contracts=None):
        if underlyings:
            ts = pd.to_datetime(["2026-01-02T14:50:00Z"] * 4, utc=True)
            return pd.DataFrame(
                {
                    "contract_symbol": [
                        "AAPL260220C00100000",
                        CONTRACT,
                        "AAPL260220P00100000",
                        "AAPL260220P00090000",
                    ],
                    "underlying": ["AAPL.US"] * 4,
                    "event_ts": ts,
                    "expiration": pd.to_datetime(["2026-02-20T21:00:00Z"] * 4, utc=True),
                    "strike": [100.0, 110.0, 100.0, 90.0],
                    "option_type": ["call", "call", "put", "put"],
                    "bid": [5.0, 1.9, 4.7, 1.6],
                    "ask": [5.2, 2.0, 4.9, 1.8],
                    "volume": [100, 100, 100, 100],
                    "open_interest": [1000, 800, 900, 700],
                    "implied_volatility": [0.50, 0.55, 0.52, 0.60],
                    "source": ["test"] * 4,
                    "available_at": ts,
                }
            )
        if contracts:
            ts = pd.to_datetime(
                [
                    "2026-01-02T15:00:00Z",
                    "2026-01-03T15:00:00Z",
                    "2026-01-06T15:00:00Z",
                ],
                utc=True,
            )
            return pd.DataFrame(
                {
                    "contract_symbol": [CONTRACT] * 3,
                    "underlying": ["AAPL.US"] * 3,
                    "event_ts": ts,
                    "expiration": pd.to_datetime(["2026-02-20T21:00:00Z"] * 3, utc=True),
                    "strike": [110.0] * 3,
                    "option_type": ["call"] * 3,
                    "bid": [2.0, 4.0, 8.2],
                    "ask": [2.1, 4.1, 8.3],
                    "volume": [100] * 3,
                    "open_interest": [800] * 3,
                    "implied_volatility": [0.55] * 3,
                    "source": ["test"] * 3,
                    "available_at": ts,
                }
            )
        return pd.DataFrame()


class NoHistoricalIVStore(FakeStore):
    def option_quotes_asof(self, **kwargs):
        frame = super().option_quotes_asof(**kwargs)
        if kwargs.get("underlyings") and not frame.empty:
            frame = frame.copy()
            frame["implied_volatility"] = None
        return frame


def _selection(symbols_seen: list[list[str]]):
    def fake_replay(store, symbols, *, research_time, config):
        symbols_seen.append(list(symbols))
        return {
            "equity_frames_available": 1,
            "option_quotes_in_lookback": 4,
            "data_completeness_warnings": [],
            "chart_stage": {"candidate_count": 1},
            "final_stage": {
                "decision": "RESEARCH_CANDIDATES",
                "candidates": [
                    {
                        "symbol": "AAPL.US",
                        "contract_symbol": CONTRACT,
                        "direction": "bullish",
                        "setup_type": "breakout",
                        "final_rank": 1,
                        "ranking_score": 82.0,
                        "option_score": 80.0,
                        "required_move_vs_one_sigma": 1.1,
                        "required_underlying_move_pct": 20.0,
                        "spot": 100.0,
                        "strike": 110.0,
                        "expiration": "2026-02-20T21:00:00+00:00",
                        "dte": 49,
                        "bid": 1.9,
                        "entry_ask": 2.0,
                        "spread_pct": 5.13,
                        "open_interest": 800,
                        "volume": 100,
                        "implied_volatility": 0.55,
                        "realized_vol20_pct": 30.0,
                    }
                ],
            },
        }
    return fake_replay


def test_static_universe_requires_explicit_survivorship_acknowledgement(monkeypatch) -> None:
    monkeypatch.setattr(experiments, "replay_selection_at", _selection([]))
    with pytest.raises(ValueError, match="survivorship bias"):
        run_historical_research_experiment(
            FakeStore(),
            [RESEARCH],
            evaluation_as_of=EVALUATION,
            static_symbols=["AAPL.US"],
        )


def test_static_project_symbol_is_not_stripped_and_run_is_labeled(monkeypatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(experiments, "replay_selection_at", _selection(seen))
    report = run_historical_research_experiment(
        FakeStore(),
        [RESEARCH],
        evaluation_as_of=EVALUATION,
        static_symbols=["AAPL.US"],
        experiment_config=HistoricalExperimentConfig(
            allow_static_universe=True,
            outcome_horizon_days=5,
            min_bucket_samples=1,
        ),
    )

    assert seen == [["AAPL.US"]]
    assert report["survivorship_bias_control"]["point_in_time_universe"] is False
    assert "static_universe_survivorship_bias_possible" in report["data_completeness_warnings"]
    assert "data_snapshot_id_missing_reproducibility_weaker" in report["data_completeness_warnings"]
    assert report["universe_fingerprint"].startswith("universe_")
    assert report["selected_candidate_count"] == 1
    assert report["mature_outcome_count"] == 1
    assert report["outcomes"][0]["target_hit"] is True
    assert report["outcomes"][0]["evaluation_end"].startswith("2026-01-07")
    assert report["outcomes"][0]["surface_efficiency_score"] is not None


def test_point_in_time_universe_uses_latest_snapshot_available_at_research_time(monkeypatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(experiments, "replay_selection_at", _selection(seen))
    snapshots = [
        {"available_at": "2026-01-01T20:00:00+00:00", "symbols": ["AAPL.US"], "source": "snapshot-1"},
        {"available_at": "2026-01-03T20:00:00+00:00", "symbols": ["MSFT.US"], "source": "snapshot-2"},
    ]
    report = run_historical_research_experiment(
        FakeStore(),
        [RESEARCH],
        evaluation_as_of=EVALUATION,
        universe_snapshots=snapshots,
        experiment_config=HistoricalExperimentConfig(outcome_horizon_days=5, min_bucket_samples=1),
        lineage=ExperimentLineage(data_snapshot_id="dataset-2026-02-15"),
    )

    assert seen == [["AAPL.US"]]
    assert report["universe_mode"] == "point_in_time_snapshots"
    assert report["survivorship_bias_control"]["point_in_time_universe"] is True
    assert report["sessions"][0]["universe"]["source"] == "snapshot-1"
    assert report["lineage"]["data_snapshot_id"] == "dataset-2026-02-15"
    assert "data_snapshot_id_missing_reproducibility_weaker" not in report["data_completeness_warnings"]


def test_universe_history_changes_experiment_identity(monkeypatch) -> None:
    monkeypatch.setattr(experiments, "replay_selection_at", _selection([]))
    cfg = HistoricalExperimentConfig(outcome_horizon_days=5, min_bucket_samples=1)
    lineage = ExperimentLineage(commit_sha="abc", data_snapshot_id="data-1")
    first = run_historical_research_experiment(
        FakeStore(),
        [RESEARCH],
        evaluation_as_of=EVALUATION,
        universe_snapshots=[
            {"available_at": "2026-01-01T20:00:00+00:00", "symbols": ["AAPL.US"], "source": "u1"}
        ],
        experiment_config=cfg,
        lineage=lineage,
    )
    second = run_historical_research_experiment(
        FakeStore(),
        [RESEARCH],
        evaluation_as_of=EVALUATION,
        universe_snapshots=[
            {"available_at": "2026-01-01T20:00:00+00:00", "symbols": ["AAPL.US", "MSFT.US"], "source": "u1"}
        ],
        experiment_config=cfg,
        lineage=lineage,
    )
    assert first["universe_fingerprint"] != second["universe_fingerprint"]
    assert first["experiment_id"] != second["experiment_id"]


def test_historical_iv_gap_is_explicit_and_not_invented(monkeypatch) -> None:
    monkeypatch.setattr(experiments, "replay_selection_at", _selection([]))
    report = run_historical_research_experiment(
        NoHistoricalIVStore(),
        [RESEARCH],
        evaluation_as_of=EVALUATION,
        static_symbols=["AAPL.US"],
        experiment_config=HistoricalExperimentConfig(
            allow_static_universe=True,
            outcome_horizon_days=5,
            min_bucket_samples=1,
        ),
    )
    assert "historical_implied_volatility_unavailable" in report["data_completeness_warnings"]
    assert report["selected_candidates"][0].get("surface_efficiency_score") is None


def test_outcome_is_not_labeled_before_fixed_horizon_matures(monkeypatch) -> None:
    monkeypatch.setattr(experiments, "replay_selection_at", _selection([]))
    report = run_historical_research_experiment(
        FakeStore(),
        [RESEARCH],
        evaluation_as_of=datetime(2026, 1, 5, 15, 0, tzinfo=UTC),
        static_symbols=["AAPL.US"],
        experiment_config=HistoricalExperimentConfig(
            allow_static_universe=True,
            outcome_horizon_days=5,
            min_bucket_samples=1,
        ),
    )
    assert report["mature_outcome_count"] == 0
    assert report["immature_candidate_count"] == 1


def test_experiment_id_is_deterministic_for_same_inputs(monkeypatch) -> None:
    monkeypatch.setattr(experiments, "replay_selection_at", _selection([]))
    kwargs = dict(
        evaluation_as_of=EVALUATION,
        static_symbols=["AAPL.US"],
        experiment_config=HistoricalExperimentConfig(allow_static_universe=True, outcome_horizon_days=5),
        lineage=ExperimentLineage(commit_sha="abc123", data_snapshot_id="snapshot-123"),
    )
    first = run_historical_research_experiment(FakeStore(), [RESEARCH], **kwargs)
    second = run_historical_research_experiment(FakeStore(), [RESEARCH], **kwargs)
    assert first["experiment_id"] == second["experiment_id"]


def test_summary_marks_small_buckets_invalid() -> None:
    summary = summarize_historical_experiment(
        [
            {
                "direction": "bullish",
                "target_hit": True,
                "touch_2x": True,
                "touch_3x": False,
                "full_loss_proxy": False,
                "end_return_pct": 100.0,
                "max_multiple": 4.0,
            }
        ],
        min_bucket_samples=30,
    )
    assert summary["samples"] == 1
    assert summary["overall"]["valid_sample"] is False
    assert summary["dimensions"]["direction"][0]["bucket"] == "bullish"
