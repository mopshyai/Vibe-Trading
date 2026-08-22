"""Build live-candidate EV and walk-forward artifacts from labeled history.

Historical outcome rows are evaluation data: they are eligible only when their
``evaluation_as_of`` timestamp is no later than the evidence timestamp. This
keeps a replay labeled with future quotes from leaking into an earlier decision.

For each current candidate we:
- use same-option-type historical outcomes (calls with calls, puts with puts),
- estimate local empirical EV from a symmetric score neighborhood,
- evaluate the score-threshold selection process with embargoed walk-forward,
- train the final score threshold on all eligible history,
- require the current ranking score to clear that trained threshold.

The output remains evidence, never a guarantee or broker instruction.
"""

from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd

from .ev import ExpectedValueConfig, candidate_ev_gate, choose_score_threshold
from .walkforward import WalkForwardConfig, evaluate_walk_forward

UTC = timezone.utc


def build_candidate_evidence_artifacts(
    candidates: Sequence[Mapping[str, Any]],
    outcomes: Iterable[Mapping[str, Any]],
    *,
    as_of: datetime,
    score_field: str = "ranking_score",
    score_width: float = 10.0,
    ev_config: ExpectedValueConfig | None = None,
    walk_config: WalkForwardConfig | None = None,
) -> dict[str, Any]:
    """Return exact-candidate EV/walk-forward maps for the personal gate."""
    reference = _aware(as_of, "as_of")
    ecfg = ev_config or ExpectedValueConfig()
    wcfg = walk_config or WalkForwardConfig()
    ecfg.validate()
    wcfg.validate()
    eligible, rejected = _eligible_outcomes(outcomes, as_of=reference)

    ev_reports: dict[str, dict[str, Any]] = {}
    walk_reports: dict[str, dict[str, Any]] = {}
    diagnostics: list[dict[str, Any]] = []

    for raw_candidate in candidates:
        candidate = dict(raw_candidate)
        key = _candidate_key(candidate)
        if not key:
            continue
        option_type = str(candidate.get("option_type") or "").strip().lower()
        pool = [
            row for row in eligible
            if not option_type or str(row.get("option_type") or "").strip().lower() == option_type
        ]
        ev = candidate_ev_gate(
            candidate,
            pool,
            score_field=score_field,
            score_width=score_width,
            config=ecfg,
        )
        walk = evaluate_walk_forward(
            pool,
            timestamp_field="entry_time",
            score_field=score_field,
            walk_config=wcfg,
            ev_config=ecfg,
        )
        threshold_selection = choose_score_threshold(
            pool,
            score_field=score_field,
            config=ecfg,
        )
        threshold = _finite(threshold_selection.get("threshold"))
        candidate_score = _finite(candidate.get(score_field))
        score_gate_pass = (
            threshold is not None
            and candidate_score is not None
            and candidate_score >= threshold
        )
        base_walk_decision = str(walk.get("decision") or "")
        final_walk_pass = base_walk_decision == "WALK_FORWARD_PASS" and score_gate_pass
        walk = {
            **walk,
            "base_walk_forward_decision": base_walk_decision,
            "selected_training_threshold": threshold,
            "threshold_selection_decision": threshold_selection.get("decision"),
            "candidate_score": candidate_score,
            "candidate_score_gate_pass": score_gate_pass,
            "decision": (
                "WALK_FORWARD_PASS"
                if final_walk_pass
                else "WALK_FORWARD_INSUFFICIENT_OR_FAIL"
            ),
            "evidence_as_of": reference.isoformat(),
            "historical_pool_samples": len(pool),
        }
        ev = {
            **ev,
            "evidence_as_of": reference.isoformat(),
            "historical_pool_samples": len(pool),
            "option_type_pool": option_type or "all",
        }
        ev_reports[key] = ev
        walk_reports[key] = walk
        diagnostics.append(
            {
                "key": key,
                "symbol": candidate.get("symbol") or candidate.get("ticker"),
                "option_type": option_type or None,
                "candidate_score": candidate_score,
                "historical_pool_samples": len(pool),
                "ev_positive": bool(ev.get("positive_ev")),
                "ev_samples": int(ev.get("samples") or 0),
                "walk_base_decision": base_walk_decision,
                "trained_threshold": threshold,
                "candidate_score_gate_pass": score_gate_pass,
                "walk_final_decision": walk["decision"],
            }
        )

    return {
        "schema_version": 1,
        "mode": "candidate_empirical_evidence",
        "as_of": reference.isoformat(),
        "eligible_historical_outcomes": len(eligible),
        "rejected_historical_outcomes": rejected,
        "candidate_count": len(diagnostics),
        "ev_reports": ev_reports,
        "walkforward_reports": walk_reports,
        "diagnostics": diagnostics,
        "warning": (
            "Historical evidence is not a return forecast. A PASS requires enough point-in-time history, "
            "positive conservative EV, embargoed walk-forward stability, and the current score to clear "
            "the final threshold trained only on history available by evidence_as_of."
        ),
    }


def _eligible_outcomes(
    outcomes: Iterable[Mapping[str, Any]],
    *,
    as_of: datetime,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    eligible: list[dict[str, Any]] = []
    rejected = {
        "missing_entry_time": 0,
        "missing_evaluation_as_of": 0,
        "future_entry": 0,
        "future_evaluation": 0,
        "missing_outcome_label": 0,
    }
    for raw in outcomes:
        row = dict(raw)
        entry = _timestamp(row.get("entry_time"))
        evaluation = _timestamp(row.get("evaluation_as_of"))
        if entry is None:
            rejected["missing_entry_time"] += 1
            continue
        if evaluation is None:
            rejected["missing_evaluation_as_of"] += 1
            continue
        if entry > as_of:
            rejected["future_entry"] += 1
            continue
        if evaluation > as_of:
            rejected["future_evaluation"] += 1
            continue
        if "target_hit" not in row or row.get("end_return_pct") is None:
            rejected["missing_outcome_label"] += 1
            continue
        row["entry_time"] = entry.isoformat()
        row["evaluation_as_of"] = evaluation.isoformat()
        eligible.append(row)
    return eligible, rejected


def _candidate_key(candidate: Mapping[str, Any]) -> str:
    contract = str(candidate.get("contract_symbol") or "").strip().upper()
    if contract:
        return contract
    return str(candidate.get("symbol") or candidate.get("ticker") or "").strip().upper()


def _timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        return None
    return pd.Timestamp(parsed).to_pydatetime().astimezone(UTC)


def _aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _finite(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


__all__ = ["build_candidate_evidence_artifacts"]
