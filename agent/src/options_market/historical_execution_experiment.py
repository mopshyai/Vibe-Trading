"""Apply retrospective limit-order execution realism to historical experiments.

Entry-path quote queries are capped at the simulated order timeout in both event
and availability time. A correction received after that timeout cannot
retroactively create a fill. Outcome paths are downstream evaluation labels and
may use the experiment's later evaluation timestamp.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import math
from typing import Any, Mapping

import pandas as pd

from .historical_execution import (
    HistoricalExecutionConfig,
    label_filled_execution_outcome,
    simulate_long_option_entry,
    summarize_historical_execution,
)
from .outcomes import OutcomeConfig
from .store import OptionsResearchStore

UTC = timezone.utc


def apply_execution_model_to_experiment(
    store: OptionsResearchStore,
    experiment: Mapping[str, Any],
    *,
    config: HistoricalExecutionConfig | None = None,
) -> dict[str, Any]:
    """Re-evaluate selected candidates through a historical buy-limit path."""
    cfg = config or HistoricalExecutionConfig()
    cfg.validate()
    evaluation_as_of = _time(experiment.get("created_as_of"))
    if evaluation_as_of is None:
        raise ValueError("experiment created_as_of timezone-aware timestamp is required")

    experiment_config = _mapping(experiment.get("experiment_config"))
    replay_config = _mapping(experiment.get("replay_config"))
    horizon_days = _positive_int(experiment_config.get("outcome_horizon_days")) or 30
    target_profit_pct = _positive(replay_config.get("target_profit_pct")) or 300.0
    selected = experiment.get("selected_candidates")
    if not isinstance(selected, list):
        raise ValueError("experiment selected_candidates list is required")

    original_outcomes = {
        key: dict(row)
        for row in experiment.get("outcomes", [])
        if isinstance(row, Mapping) and (key := _candidate_key(row)) is not None
    }

    results: list[dict[str, Any]] = []
    for raw in selected:
        if not isinstance(raw, Mapping):
            continue
        candidate = dict(raw)
        key = _candidate_key(candidate)
        contract = str(candidate.get("contract_symbol") or "").strip().upper().replace(" ", "")
        decision_time = _candidate_time(candidate)
        if not contract or decision_time is None:
            execution = simulate_long_option_entry(candidate, pd.DataFrame(), config=cfg)
            results.append(
                {
                    "candidate_key": key,
                    "candidate": candidate,
                    "execution": execution,
                    "outcome": None,
                }
            )
            continue

        order_expires_at = decision_time + timedelta(seconds=cfg.max_wait_seconds)
        entry_quotes = store.option_quotes_asof(
            as_of=order_expires_at,
            start=decision_time,
            end=order_expires_at,
            contracts=[contract],
        )
        execution = simulate_long_option_entry(candidate, entry_quotes, config=cfg)

        outcome = None
        if bool(execution.get("filled")):
            original = original_outcomes.get(key or "")
            evaluation_end = _time(original.get("evaluation_end")) if original else None
            if evaluation_end is None:
                expiration = _time(candidate.get("expiration"))
                horizon_end = decision_time + timedelta(days=horizon_days)
                evaluation_end = min(horizon_end, expiration) if expiration is not None else horizon_end
            evaluation_end = min(evaluation_end, evaluation_as_of)
            fill_time = _time(execution.get("fill_time"))
            if fill_time is not None and evaluation_end >= fill_time:
                outcome_quotes = store.option_quotes_asof(
                    as_of=evaluation_as_of,
                    start=fill_time,
                    end=evaluation_end,
                    contracts=[contract],
                )
                outcome = label_filled_execution_outcome(
                    candidate,
                    execution,
                    outcome_quotes,
                    outcome_config=OutcomeConfig(target_profit_pct=target_profit_pct),
                )
                if outcome is not None:
                    outcome["evaluation_end"] = evaluation_end.isoformat()
                    outcome["evaluation_as_of"] = evaluation_as_of.isoformat()

        results.append(
            {
                "candidate_key": key,
                "candidate": candidate,
                "execution": execution,
                "outcome": outcome,
            }
        )

    execution_rows = [dict(row["execution"]) for row in results]
    filled_outcomes = [
        dict(row["outcome"])
        for row in results
        if isinstance(row.get("outcome"), Mapping)
    ]
    return {
        "mode": "historical_execution_adjusted_experiment",
        "source_experiment_id": experiment.get("experiment_id"),
        "evaluation_as_of": evaluation_as_of.isoformat(),
        "execution_config": asdict(cfg),
        "candidate_results": results,
        "execution_summary": summarize_historical_execution(execution_rows),
        "filled_outcome_summary": _outcome_summary(filled_outcomes),
        "evaluation_only": True,
        "broker_mutation": False,
        "warning": (
            "Quote-trigger execution model only. Without queue position, quote sizes, trades and full order-book history, a triggered limit is not proof that a real order would have filled."
        ),
    }


def _outcome_summary(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    end_returns = [
        value
        for row in rows
        if (value := _finite(row.get("end_return_pct"))) is not None
    ]
    return {
        "filled_outcomes": n,
        "target_hit_rate": 0.0 if n == 0 else round(sum(bool(row.get("target_hit")) for row in rows) / n, 4),
        "touch_2x_rate": 0.0 if n == 0 else round(sum(bool(row.get("touch_2x")) for row in rows) / n, 4),
        "full_loss_proxy_rate": 0.0 if n == 0 else round(sum(bool(row.get("full_loss_proxy")) for row in rows) / n, 4),
        "mean_end_return_pct": None if not end_returns else round(sum(end_returns) / len(end_returns), 4),
    }


def _candidate_key(row: Mapping[str, Any]) -> str | None:
    contract = str(row.get("contract_symbol") or "").strip().upper().replace(" ", "")
    research_time = _time(row.get("research_time") or row.get("entry_time"))
    if not contract or research_time is None:
        return None
    return f"{contract}|{research_time.isoformat()}"


def _candidate_time(row: Mapping[str, Any]) -> datetime | None:
    return _time(row.get("research_time") or row.get("entry_time") or row.get("decision_time"))


def _time(value: object) -> datetime | None:
    if value is None:
        return None
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        return None
    return pd.Timestamp(parsed).to_pydatetime().astimezone(UTC)


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _finite(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _positive(value: object) -> float | None:
    number = _finite(value)
    return number if number is not None and number > 0 else None


def _positive_int(value: object) -> int | None:
    number = _finite(value)
    if number is None or number <= 0 or int(number) != number:
        return None
    return int(number)


__all__ = ["apply_execution_model_to_experiment"]
