"""Empirical payoff-target selection for personal long-option research.

The personal system should not force every setup into a +300% profit objective.
This module compares several target/exit policies using historical maximum option
multiples and ending bid returns, then selects a target only when the conservative
lower confidence bound remains positive.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from statistics import NormalDist
from typing import Any, Iterable, Mapping, Sequence

import numpy as np


@dataclass(frozen=True)
class PayoffPolicyConfig:
    target_profit_pcts: tuple[float, ...] = (100.0, 200.0, 300.0)
    min_samples: int = 50
    confidence_level: float = 0.90
    min_expected_return_pct: float = 0.0
    min_lower_confidence_bound_pct: float = 0.0
    min_failure_return_pct: float = -100.0
    max_failure_return_pct: float = 0.0

    def validate(self) -> None:
        if not self.target_profit_pcts:
            raise ValueError("target_profit_pcts cannot be empty")
        if any(not 0 < float(target) <= 10_000 for target in self.target_profit_pcts):
            raise ValueError("every target_profit_pct must be > 0 and <= 10000")
        if len(set(float(target) for target in self.target_profit_pcts)) != len(self.target_profit_pcts):
            raise ValueError("target_profit_pcts must be unique")
        if self.min_samples < 1:
            raise ValueError("min_samples must be positive")
        if not 0.50 < self.confidence_level < 1.0:
            raise ValueError("confidence_level must be between 0.50 and 1")
        if self.min_failure_return_pct < -100:
            raise ValueError("min_failure_return_pct cannot be below -100")
        if self.max_failure_return_pct < self.min_failure_return_pct:
            raise ValueError("failure-return bounds are inverted")


def evaluate_payoff_targets(
    outcomes: Iterable[Mapping[str, Any]],
    *,
    config: PayoffPolicyConfig | None = None,
) -> dict[str, Any]:
    """Compare target-or-end policies and select the strongest robust target.

    ``max_multiple`` is used only because this function operates on already
    labeled historical outcomes. It must never be used as a live feature.
    """
    cfg = config or PayoffPolicyConfig()
    cfg.validate()
    rows = [dict(row) for row in outcomes]
    evaluations = [
        _evaluate_target(rows, float(target), cfg)
        for target in sorted(float(value) for value in cfg.target_profit_pcts)
    ]
    passing = [row for row in evaluations if row["positive_ev"]]
    if not passing:
        return {
            "decision": "NO_PAYOFF_POLICY",
            "selected_target_profit_pct": None,
            "selected_target_multiple": None,
            "evaluations": evaluations,
            "config": asdict(cfg),
            "warning": (
                "No tested payoff target demonstrated sufficiently robust positive historical EV. "
                "Do not increase the target merely to create a more exciting payoff."
            ),
        }

    passing.sort(
        key=lambda row: (
            -float(row["lower_confidence_bound_pct"]),
            -float(row["expected_return_pct"]),
            -int(row["samples"]),
            float(row["target_profit_pct"]),
        )
    )
    selected = passing[0]
    return {
        "decision": "PAYOFF_POLICY_SELECTED",
        "selected_target_profit_pct": selected["target_profit_pct"],
        "selected_target_multiple": selected["target_multiple"],
        "selected": selected,
        "evaluations": evaluations,
        "config": asdict(cfg),
        "warning": (
            "Historical payoff-policy selection is not a forecast. Select on training data and validate on later "
            "unseen periods before using it in a current decision."
        ),
    }


def _evaluate_target(
    rows: Sequence[Mapping[str, Any]],
    target_profit_pct: float,
    cfg: PayoffPolicyConfig,
) -> dict[str, Any]:
    target_multiple = 1.0 + target_profit_pct / 100.0
    policy_returns: list[float] = []
    hits: list[bool] = []
    for row in rows:
        max_multiple = _finite(row.get("max_multiple"))
        ending = _finite(row.get("end_return_pct"))
        if max_multiple is None or ending is None:
            continue
        hit = max_multiple >= target_multiple
        if hit:
            policy_return = target_profit_pct
        else:
            policy_return = min(cfg.max_failure_return_pct, max(cfg.min_failure_return_pct, ending))
        hits.append(hit)
        policy_returns.append(policy_return)

    samples = len(policy_returns)
    if samples == 0:
        return {
            "target_profit_pct": target_profit_pct,
            "target_multiple": target_multiple,
            "samples": 0,
            "target_hit_rate": 0.0,
            "expected_return_pct": None,
            "return_std_pct": None,
            "lower_confidence_bound_pct": None,
            "upper_confidence_bound_pct": None,
            "positive_ev": False,
        }

    values = np.asarray(policy_returns, dtype=float)
    mean = float(np.mean(values))
    std = float(np.std(values, ddof=1)) if samples > 1 else 0.0
    se = std / math.sqrt(samples)
    z = NormalDist().inv_cdf((1.0 + cfg.confidence_level) / 2.0)
    lower = mean - z * se
    upper = mean + z * se
    positive = (
        samples >= cfg.min_samples
        and mean >= cfg.min_expected_return_pct
        and lower >= cfg.min_lower_confidence_bound_pct
    )
    return {
        "target_profit_pct": target_profit_pct,
        "target_multiple": target_multiple,
        "samples": samples,
        "target_hit_rate": round(float(np.mean(np.asarray(hits, dtype=bool))), 4),
        "expected_return_pct": round(mean, 4),
        "return_std_pct": round(std, 4),
        "lower_confidence_bound_pct": round(lower, 4),
        "upper_confidence_bound_pct": round(upper, 4),
        "positive_ev": positive,
    }


def _finite(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


__all__ = ["PayoffPolicyConfig", "evaluate_payoff_targets"]
