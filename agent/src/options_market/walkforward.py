"""Embargoed walk-forward evaluation for the options research stack.

The evaluator trains score thresholds only on observations that precede each test
window, applies an embargo between train and test, and reports realized
out-of-sample target-or-exit returns.  It is intentionally simple enough to
inspect: no random cross-validation and no shuffling of time-series outcomes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

from .ev import ExpectedValueConfig, choose_score_threshold, empirical_expected_value


@dataclass(frozen=True)
class WalkForwardConfig:
    """Calendar settings for expanding-window evaluation."""

    min_train_days: int = 365
    test_days: int = 63
    step_days: int = 63
    embargo_days: int = 5
    minimum_folds: int = 2

    def validate(self) -> None:
        if self.min_train_days < 30:
            raise ValueError("min_train_days must be at least 30")
        if self.test_days < 1:
            raise ValueError("test_days must be positive")
        if self.step_days < 1:
            raise ValueError("step_days must be positive")
        if self.embargo_days < 0:
            raise ValueError("embargo_days cannot be negative")
        if self.minimum_folds < 1:
            raise ValueError("minimum_folds must be at least 1")


def evaluate_walk_forward(
    outcomes: Iterable[Mapping[str, Any]],
    *,
    timestamp_field: str = "entry_time",
    score_field: str = "ranking_score",
    walk_config: WalkForwardConfig | None = None,
    ev_config: ExpectedValueConfig | None = None,
) -> dict[str, Any]:
    """Evaluate score-threshold selection on later unseen observations."""
    wcfg = walk_config or WalkForwardConfig()
    ecfg = ev_config or ExpectedValueConfig()
    wcfg.validate()
    ecfg.validate()

    frame = pd.DataFrame([dict(row) for row in outcomes])
    if frame.empty or timestamp_field not in frame.columns:
        return _empty(wcfg, "No timestamped outcomes supplied.")
    frame["_ts"] = pd.to_datetime(frame[timestamp_field], utc=True, errors="coerce")
    frame = frame.dropna(subset=["_ts"]).sort_values("_ts").reset_index(drop=True)
    if frame.empty:
        return _empty(wcfg, "No valid outcome timestamps supplied.")

    first_ts = frame["_ts"].iloc[0]
    last_ts = frame["_ts"].iloc[-1]
    first_test_start = first_ts + pd.Timedelta(days=wcfg.min_train_days + wcfg.embargo_days)
    folds: list[dict[str, Any]] = []
    test_start = first_test_start

    while test_start <= last_ts:
        train_cutoff = test_start - pd.Timedelta(days=wcfg.embargo_days)
        test_end = test_start + pd.Timedelta(days=wcfg.test_days)
        train = frame[frame["_ts"] < train_cutoff]
        test = frame[(frame["_ts"] >= test_start) & (frame["_ts"] < test_end)]
        if not test.empty:
            selection = choose_score_threshold(
                train.to_dict(orient="records"),
                score_field=score_field,
                config=ecfg,
            )
            threshold = selection.get("threshold")
            if threshold is None:
                selected_test = test.iloc[0:0]
                report = empirical_expected_value([], config=ecfg)
                decision = "NO_TRADE"
            else:
                numeric_score = pd.to_numeric(test.get(score_field), errors="coerce")
                selected_test = test[numeric_score >= float(threshold)]
                report = empirical_expected_value(selected_test.to_dict(orient="records"), config=ecfg)
                decision = "TEST_TRADES" if not selected_test.empty else "NO_TRADE"

            folds.append(
                {
                    "fold": len(folds) + 1,
                    "train_start": train["_ts"].iloc[0].isoformat() if not train.empty else None,
                    "train_end_exclusive": train_cutoff.isoformat(),
                    "embargo_days": wcfg.embargo_days,
                    "test_start": test_start.isoformat(),
                    "test_end_exclusive": test_end.isoformat(),
                    "train_samples": int(len(train)),
                    "test_samples": int(len(test)),
                    "selected_test_samples": int(len(selected_test)),
                    "selected_threshold": threshold,
                    "selection_decision": selection.get("decision"),
                    "test_decision": decision,
                    "test_ev": report,
                }
            )
        test_start = test_start + pd.Timedelta(days=wcfg.step_days)

    valid_folds = [fold for fold in folds if fold["selected_test_samples"] > 0]
    aggregate_rows: list[dict[str, Any]] = []
    for fold in folds:
        threshold = fold.get("selected_threshold")
        if threshold is None:
            continue
        start = pd.Timestamp(fold["test_start"])
        end = pd.Timestamp(fold["test_end_exclusive"])
        mask = (frame["_ts"] >= start) & (frame["_ts"] < end)
        scores = pd.to_numeric(frame.get(score_field), errors="coerce")
        selected = frame[mask & (scores >= float(threshold))]
        aggregate_rows.extend(selected.to_dict(orient="records"))

    aggregate = empirical_expected_value(aggregate_rows, config=ecfg)
    sufficient = len(valid_folds) >= wcfg.minimum_folds
    return {
        "mode": "embargoed_expanding_walk_forward",
        "score_field": score_field,
        "timestamp_field": timestamp_field,
        "fold_count": len(folds),
        "folds_with_trades": len(valid_folds),
        "minimum_folds_met": sufficient,
        "decision": (
            "WALK_FORWARD_PASS"
            if sufficient and aggregate.get("positive_ev")
            else "WALK_FORWARD_INSUFFICIENT_OR_FAIL"
        ),
        "aggregate_out_of_sample_ev": aggregate,
        "folds": folds,
        "config": asdict(wcfg),
        "warning": (
            "Walk-forward results remain historical evidence, not a guarantee. Keep provider timestamps, delistings, "
            "corporate actions, transaction costs and model-version lineage point-in-time."
        ),
    }


def _empty(config: WalkForwardConfig, warning: str) -> dict[str, Any]:
    return {
        "mode": "embargoed_expanding_walk_forward",
        "fold_count": 0,
        "folds_with_trades": 0,
        "minimum_folds_met": False,
        "decision": "WALK_FORWARD_INSUFFICIENT_OR_FAIL",
        "aggregate_out_of_sample_ev": None,
        "folds": [],
        "config": asdict(config),
        "warning": warning,
    }


__all__ = ["WalkForwardConfig", "evaluate_walk_forward"]
