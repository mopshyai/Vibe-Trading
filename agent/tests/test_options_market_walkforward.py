from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.options_market.ev import ExpectedValueConfig
from src.options_market.walkforward import WalkForwardConfig, evaluate_walk_forward


def test_walk_forward_uses_only_prior_training_data_and_embargo() -> None:
    start = datetime(2023, 1, 1, tzinfo=timezone.utc)
    outcomes = []
    for i in range(900):
        high = i % 4 == 0
        outcomes.append(
            {
                "entry_time": (start + timedelta(days=i)).isoformat(),
                "ranking_score": 90.0 if high else 60.0,
                "target_hit": high,
                "end_return_pct": -60.0,
            }
        )

    result = evaluate_walk_forward(
        outcomes,
        walk_config=WalkForwardConfig(
            min_train_days=365,
            test_days=63,
            step_days=63,
            embargo_days=5,
            minimum_folds=2,
        ),
        ev_config=ExpectedValueConfig(
            min_samples=20,
            min_lower_confidence_bound_pct=0.0,
        ),
    )

    assert result["fold_count"] >= 2
    assert result["folds_with_trades"] >= 2
    assert result["decision"] == "WALK_FORWARD_PASS"
    for fold in result["folds"]:
        assert fold["embargo_days"] == 5
        assert fold["train_end_exclusive"] < fold["test_start"]
        if fold["selected_threshold"] is not None:
            assert fold["selected_threshold"] >= 65.0


def test_walk_forward_fails_when_there_is_not_enough_history() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    outcomes = [
        {
            "entry_time": (start + timedelta(days=i)).isoformat(),
            "ranking_score": 90.0,
            "target_hit": True,
            "end_return_pct": 300.0,
        }
        for i in range(30)
    ]
    result = evaluate_walk_forward(
        outcomes,
        walk_config=WalkForwardConfig(min_train_days=365, minimum_folds=2),
        ev_config=ExpectedValueConfig(min_samples=5),
    )
    assert result["decision"] == "WALK_FORWARD_INSUFFICIENT_OR_FAIL"
    assert result["fold_count"] == 0
