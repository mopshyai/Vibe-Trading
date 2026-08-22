"""Empirical expected-value gates for long-premium options research.

The functions in this module operate on historical outcome labels.  They do not
pretend that a heuristic score is a probability.  A candidate is considered
empirically actionable only after enough out-of-sample observations exist and a
conservative lower confidence bound on the target-or-exit policy remains above
zero (or another configured hurdle).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from statistics import NormalDist
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class ExpectedValueConfig:
    """Conservative empirical EV settings."""

    target_profit_pct: float = 300.0
    min_samples: int = 50
    confidence_level: float = 0.90
    min_expected_return_pct: float = 0.0
    min_lower_confidence_bound_pct: float = 0.0
    max_failure_return_pct: float = 0.0
    min_failure_return_pct: float = -100.0

    def validate(self) -> None:
        if not 0 < self.target_profit_pct <= 10_000:
            raise ValueError("target_profit_pct must be > 0 and <= 10000")
        if self.min_samples < 1:
            raise ValueError("min_samples must be at least 1")
        if not 0.50 < self.confidence_level < 1.0:
            raise ValueError("confidence_level must be between 0.50 and 1")
        if self.min_failure_return_pct < -100:
            raise ValueError("min_failure_return_pct cannot be below -100")
        if self.max_failure_return_pct > self.target_profit_pct:
            raise ValueError("max_failure_return_pct cannot exceed target_profit_pct")
        if self.max_failure_return_pct < self.min_failure_return_pct:
            raise ValueError("failure return bounds are inverted")


def empirical_expected_value(
    outcomes: Iterable[Mapping[str, Any]],
    *,
    config: ExpectedValueConfig | None = None,
) -> dict[str, Any]:
    """Estimate a target-or-last-bid policy from historical outcomes.

    Policy economics:
    - if the 4x target was touched, count +300% (or configured target), assuming
      a standing/triggered exit could obtain the target bid;
    - otherwise use the observed ending bid return, bounded to [-100%, target].

    This is intentionally simpler and more conservative than using maximum
    favorable excursion as if it were always captured.
    """
    cfg = config or ExpectedValueConfig()
    cfg.validate()
    rows = _valid_rows(outcomes, cfg)
    if not rows:
        return _empty_result(cfg, "No valid historical outcomes supplied.")

    returns = np.asarray([row["policy_return_pct"] for row in rows], dtype=float)
    hits = np.asarray([row["target_hit"] for row in rows], dtype=bool)
    sample_size = int(len(rows))
    mean_return = float(np.mean(returns))
    median_return = float(np.median(returns))
    standard_deviation = float(np.std(returns, ddof=1)) if sample_size > 1 else 0.0
    standard_error = standard_deviation / math.sqrt(sample_size) if sample_size else math.inf
    z = NormalDist().inv_cdf((1.0 + cfg.confidence_level) / 2.0)
    lower = mean_return - z * standard_error
    upper = mean_return + z * standard_error
    hit_rate = float(np.mean(hits))
    loss_rate = float(np.mean(returns < 0.0))
    wipeout_rate = float(np.mean(returns <= -95.0))

    valid = sample_size >= cfg.min_samples
    positive = (
        valid
        and mean_return >= cfg.min_expected_return_pct
        and lower >= cfg.min_lower_confidence_bound_pct
    )

    return {
        "samples": sample_size,
        "target_profit_pct": cfg.target_profit_pct,
        "target_multiple": 1.0 + cfg.target_profit_pct / 100.0,
        "empirical_target_hit_rate": round(hit_rate, 4),
        "empirical_loss_rate": round(loss_rate, 4),
        "empirical_near_wipeout_rate": round(wipeout_rate, 4),
        "expected_return_pct": round(mean_return, 4),
        "median_policy_return_pct": round(median_return, 4),
        "return_std_pct": round(standard_deviation, 4),
        "standard_error_pct": round(standard_error, 4),
        "confidence_level": cfg.confidence_level,
        "lower_confidence_bound_pct": round(lower, 4),
        "upper_confidence_bound_pct": round(upper, 4),
        "calibration_valid": valid,
        "positive_ev": positive,
        "decision": "PASS_EV_GATE" if positive else "REJECT_EV_GATE",
        "config": asdict(cfg),
        "warning": (
            "Historical expected value is not a guaranteed future return. Use point-in-time, out-of-sample data and "
            "re-check stability across market regimes, spreads, slippage, and execution latency."
        ),
    }


def candidate_ev_gate(
    candidate: Mapping[str, Any],
    outcomes: Iterable[Mapping[str, Any]],
    *,
    score_field: str = "ranking_score",
    score_width: float = 10.0,
    config: ExpectedValueConfig | None = None,
) -> dict[str, Any]:
    """Evaluate historical peers near a candidate's score.

    The neighborhood is symmetric around the candidate score and never uses the
    candidate's future outcome.  Callers running historical evaluation must pass
    training-only outcomes to preserve the walk-forward boundary.
    """
    cfg = config or ExpectedValueConfig()
    cfg.validate()
    if score_width <= 0 or score_width > 100:
        raise ValueError("score_width must be > 0 and <= 100")
    score = _finite(candidate.get(score_field))
    if score is None or not 0 <= score <= 100:
        return {
            "decision": "REJECT_EV_GATE",
            "reason": f"candidate missing valid {score_field}",
            "positive_ev": False,
        }

    lower_score = max(0.0, score - score_width / 2.0)
    upper_score = min(100.0, score + score_width / 2.0)
    peers = []
    for raw in outcomes:
        peer_score = _finite(raw.get(score_field))
        if peer_score is None:
            continue
        if lower_score <= peer_score <= upper_score:
            peers.append(raw)

    result = empirical_expected_value(peers, config=cfg)
    return {
        **result,
        "candidate_score": round(score, 4),
        "peer_score_min": round(lower_score, 4),
        "peer_score_max": round(upper_score, 4),
    }


def choose_score_threshold(
    outcomes: Iterable[Mapping[str, Any]],
    *,
    score_field: str = "ranking_score",
    thresholds: Iterable[float] = tuple(range(50, 96, 5)),
    config: ExpectedValueConfig | None = None,
) -> dict[str, Any]:
    """Choose the lowest score threshold with robust positive historical EV.

    Among thresholds that pass the configured lower-confidence-bound hurdle, the
    winner maximizes the lower confidence bound; ties prefer more observations
    and then the higher threshold.  This keeps the selection rule explicit and
    testable instead of hand-tuning a magic score cutoff.
    """
    cfg = config or ExpectedValueConfig()
    cfg.validate()
    raw_rows = [dict(row) for row in outcomes]
    evaluations: list[dict[str, Any]] = []
    for raw_threshold in thresholds:
        threshold = float(raw_threshold)
        if not 0 <= threshold <= 100:
            continue
        subset = [
            row for row in raw_rows
            if (_finite(row.get(score_field)) is not None and float(row[score_field]) >= threshold)
        ]
        report = empirical_expected_value(subset, config=cfg)
        evaluations.append({"threshold": threshold, **report})

    passing = [row for row in evaluations if row.get("positive_ev")]
    if not passing:
        return {
            "decision": "NO_THRESHOLD",
            "score_field": score_field,
            "threshold": None,
            "evaluations": evaluations,
            "warning": "No tested score threshold demonstrated sufficiently robust positive historical EV.",
        }

    passing.sort(
        key=lambda row: (
            -float(row["lower_confidence_bound_pct"]),
            -int(row["samples"]),
            -float(row["threshold"]),
        )
    )
    best = passing[0]
    return {
        "decision": "THRESHOLD_SELECTED",
        "score_field": score_field,
        "threshold": best["threshold"],
        "selected": best,
        "evaluations": evaluations,
        "warning": "Threshold was selected from historical training data and must be tested only on later unseen data.",
    }


def _valid_rows(outcomes: Iterable[Mapping[str, Any]], cfg: ExpectedValueConfig) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in outcomes:
        if "target_hit" not in raw:
            continue
        target_hit = _bool(raw.get("target_hit"))
        if target_hit is None:
            continue
        if target_hit:
            policy_return = cfg.target_profit_pct
        else:
            ending = _finite(raw.get("end_return_pct"))
            if ending is None:
                continue
            policy_return = min(cfg.max_failure_return_pct, max(cfg.min_failure_return_pct, ending))
        rows.append({"target_hit": target_hit, "policy_return_pct": policy_return})
    return rows


def _empty_result(cfg: ExpectedValueConfig, warning: str) -> dict[str, Any]:
    return {
        "samples": 0,
        "target_profit_pct": cfg.target_profit_pct,
        "target_multiple": 1.0 + cfg.target_profit_pct / 100.0,
        "calibration_valid": False,
        "positive_ev": False,
        "decision": "REJECT_EV_GATE",
        "warning": warning,
        "config": asdict(cfg),
    }


def _finite(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        token = value.strip().lower()
        if token in {"true", "1", "yes"}:
            return True
        if token in {"false", "0", "no"}:
            return False
    return None


__all__ = [
    "ExpectedValueConfig",
    "candidate_ev_gate",
    "choose_score_threshold",
    "empirical_expected_value",
]
