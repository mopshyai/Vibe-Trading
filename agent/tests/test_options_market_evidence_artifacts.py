from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.options_market.evidence_artifacts import build_candidate_evidence_artifacts

UTC = timezone.utc


def test_future_evaluation_labels_are_excluded_from_current_evidence() -> None:
    as_of = datetime(2026, 8, 24, 15, 0, tzinfo=UTC)
    outcomes = [
        {
            "contract_symbol": "OLD1",
            "option_type": "call",
            "entry_time": "2026-01-02T15:00:00Z",
            "evaluation_as_of": "2026-02-02T15:00:00Z",
            "ranking_score": 80.0,
            "target_hit": True,
            "end_return_pct": 300.0,
        },
        {
            "contract_symbol": "FUTURE",
            "option_type": "call",
            "entry_time": "2026-08-20T15:00:00Z",
            "evaluation_as_of": "2026-09-20T15:00:00Z",
            "ranking_score": 80.0,
            "target_hit": True,
            "end_return_pct": 300.0,
        },
    ]
    report = build_candidate_evidence_artifacts(
        [
            {
                "symbol": "TSLA",
                "contract_symbol": "TSLA260918C00400000",
                "option_type": "call",
                "ranking_score": 80.0,
            }
        ],
        outcomes,
        as_of=as_of,
    )
    assert report["eligible_historical_outcomes"] == 1
    assert report["rejected_historical_outcomes"]["future_evaluation"] == 1
    assert "TSLA260918C00400000" in report["ev_reports"]
    assert report["ev_reports"]["TSLA260918C00400000"]["samples"] == 1


def test_final_walkforward_gate_requires_current_score_to_clear_trained_threshold() -> None:
    start = datetime(2024, 1, 2, 15, 0, tzinfo=UTC)
    outcomes = []
    for day in range(620):
        entry = start + timedelta(days=day)
        outcomes.append(
            {
                "contract_symbol": f"HIST{day}",
                "option_type": "call",
                "entry_time": entry.isoformat(),
                "evaluation_as_of": (entry + timedelta(days=1)).isoformat(),
                "ranking_score": 80.0,
                "target_hit": True,
                "end_return_pct": 300.0,
            }
        )
    as_of = start + timedelta(days=700)
    key = "TSLA260918C00400000"
    report = build_candidate_evidence_artifacts(
        [
            {
                "symbol": "TSLA",
                "contract_symbol": key,
                "option_type": "call",
                "ranking_score": 75.0,
            }
        ],
        outcomes,
        as_of=as_of,
    )
    ev = report["ev_reports"][key]
    walk = report["walkforward_reports"][key]
    assert ev["positive_ev"] is True
    assert walk["base_walk_forward_decision"] == "WALK_FORWARD_PASS"
    assert walk["selected_training_threshold"] == 80.0
    assert walk["candidate_score"] == 75.0
    assert walk["candidate_score_gate_pass"] is False
    assert walk["decision"] == "WALK_FORWARD_INSUFFICIENT_OR_FAIL"


def test_call_and_put_history_are_not_pooled_together() -> None:
    start = datetime(2025, 1, 2, 15, 0, tzinfo=UTC)
    outcomes = []
    for index in range(60):
        entry = start + timedelta(days=index)
        outcomes.append(
            {
                "option_type": "call",
                "entry_time": entry.isoformat(),
                "evaluation_as_of": (entry + timedelta(days=1)).isoformat(),
                "ranking_score": 80.0,
                "target_hit": True,
                "end_return_pct": 300.0,
            }
        )
        outcomes.append(
            {
                "option_type": "put",
                "entry_time": entry.isoformat(),
                "evaluation_as_of": (entry + timedelta(days=1)).isoformat(),
                "ranking_score": 80.0,
                "target_hit": False,
                "end_return_pct": -100.0,
            }
        )
    report = build_candidate_evidence_artifacts(
        [
            {
                "symbol": "AAPL",
                "contract_symbol": "AAPL260918C00250000",
                "option_type": "call",
                "ranking_score": 80.0,
            },
            {
                "symbol": "AAPL",
                "contract_symbol": "AAPL260918P00180000",
                "option_type": "put",
                "ranking_score": 80.0,
            },
        ],
        outcomes,
        as_of=start + timedelta(days=100),
    )
    call_ev = report["ev_reports"]["AAPL260918C00250000"]
    put_ev = report["ev_reports"]["AAPL260918P00180000"]
    assert call_ev["samples"] == 60
    assert put_ev["samples"] == 60
    assert call_ev["positive_ev"] is True
    assert put_ev["positive_ev"] is False
