"""Downstream outcome checkpointing and attribution for candidate decisions.

This module is deliberately separated from the decision path. It may inspect
future option quotes only after a configured evaluation horizon has matured and
writes those observations as new ``OUTCOME_OBSERVED`` journal rows. Outcome data
must never be merged back into the historical feature row that made the trade,
watch, or rejection decision.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
import math
import re
from typing import Any, Iterable, Mapping

import pandas as pd

from src.options_market.outcomes import OutcomeConfig, label_long_option_path
from src.options_market.store import OptionsResearchStore

from .journal import JournalEntry, JournalStage
from .models import DeskDecision, PlatformEnvironment, PlatformEvent, SystemIdentity
from .store import TradingPlatformStore

UTC = timezone.utc
_OCC_RE = re.compile(r"^([A-Z0-9]{1,6})(\d{6})([CP])(\d{8})$")
_SOURCE_STAGES = {
    JournalStage.EVALUATED.value,
    JournalStage.REJECTED.value,
    JournalStage.WATCH.value,
    JournalStage.TRADE_READY.value,
}


@dataclass(frozen=True)
class AttributionConfig:
    """Rules for when and how a candidate can receive a retrospective label."""

    evaluation_horizon_days: int = 30
    target_profit_pct: float = 300.0
    full_loss_threshold_pct: float = 95.0
    minimum_quote_observations: int = 2
    max_journal_scan: int = 2000

    def validate(self) -> None:
        if not 1 <= self.evaluation_horizon_days <= 365:
            raise ValueError("evaluation_horizon_days must be between 1 and 365")
        if not 0 < self.target_profit_pct <= 10_000:
            raise ValueError("target_profit_pct must be > 0 and <= 10000")
        if not 0 < self.full_loss_threshold_pct < 100:
            raise ValueError("full_loss_threshold_pct must be between 0 and 100")
        if self.minimum_quote_observations < 1:
            raise ValueError("minimum_quote_observations must be at least 1")
        if not 1 <= self.max_journal_scan <= 2000:
            raise ValueError("max_journal_scan must be between 1 and 2000")


def checkpoint_candidate_outcomes(
    *,
    platform_store: TradingPlatformStore,
    research_store: OptionsResearchStore,
    as_of: datetime,
    config: AttributionConfig | None = None,
    system: SystemIdentity | None = None,
) -> dict[str, Any]:
    """Append mature candidate outcome labels using only retrospective data.

    The query's ``as_of`` is the later evaluation timestamp, not the historical
    decision timestamp. That is intentional: these rows are labels, never model
    features. Existing outcome rows are keyed by source journal id so repeated
    checkpoint jobs remain idempotent.
    """
    cfg = config or AttributionConfig()
    cfg.validate()
    observed_at = _aware(as_of, "as_of")
    identity = system or SystemIdentity()

    rows = platform_store.recent_journal(limit=cfg.max_journal_scan)
    already_labeled = {
        str(_mapping(row.get("metadata")).get("source_journal_id") or "").strip()
        for row in rows
        if str(row.get("stage") or "") == JournalStage.OUTCOME_OBSERVED.value
    }
    already_labeled.discard("")

    eligible = 0
    immature = 0
    labeled = 0
    skipped: list[dict[str, str]] = []
    outcomes: list[dict[str, Any]] = []

    # recent_journal is newest-first. Process source decisions oldest-first so
    # the resulting audit stream follows decision chronology within this batch.
    for source in reversed(rows):
        source_stage = str(source.get("stage") or "").strip()
        source_id = str(source.get("journal_id") or "").strip()
        if source_stage not in _SOURCE_STAGES or not source_id or source_id in already_labeled:
            continue
        contract = str(source.get("contract_symbol") or "").strip().upper()
        entry_ask = _positive(source.get("entry_ask"))
        occurred_at = _timestamp(source.get("occurred_at"))
        if not contract or entry_ask is None or occurred_at is None:
            continue
        eligible += 1

        evaluation_end = _evaluation_end(contract, occurred_at, cfg.evaluation_horizon_days)
        if evaluation_end is None:
            skipped.append({"source_journal_id": source_id, "reason": "invalid_occ_contract"})
            continue
        if observed_at < evaluation_end:
            immature += 1
            continue

        quotes = research_store.option_quotes_asof(
            as_of=observed_at,
            start=occurred_at,
            end=evaluation_end,
            contracts=[contract],
        )
        if quotes.empty or len(quotes) < cfg.minimum_quote_observations:
            skipped.append({"source_journal_id": source_id, "reason": "insufficient_future_quotes"})
            continue
        path = quotes.copy()
        path["event_ts"] = pd.to_datetime(path["event_ts"], utc=True)
        path = path.set_index("event_ts").sort_index()

        candidate = {
            "symbol": source.get("symbol"),
            "contract_symbol": contract,
            "option_type": source.get("option_type"),
            "ranking_score": source.get("ranking_score"),
            "entry_ask": entry_ask,
        }
        try:
            outcome = label_long_option_path(
                candidate,
                path,
                config=OutcomeConfig(
                    target_profit_pct=cfg.target_profit_pct,
                    full_loss_threshold_pct=cfg.full_loss_threshold_pct,
                ),
            )
        except (TypeError, ValueError) as exc:
            skipped.append({
                "source_journal_id": source_id,
                "reason": f"outcome_label_error:{type(exc).__name__}",
            })
            continue

        outcome.update(
            {
                "source_journal_id": source_id,
                "source_stage": source_stage,
                "source_decision": source.get("decision"),
                "decision_occurred_at": occurred_at.isoformat(),
                "evaluation_end": evaluation_end.isoformat(),
                "evaluated_as_of": observed_at.isoformat(),
            }
        )
        entry = _outcome_entry(source, outcome, identity=identity)
        platform_store.append_journal_entry(entry)
        platform_store.append_event(
            PlatformEvent(
                event_type="candidate_outcome_observed",
                environment=entry.environment,
                system=entry.system,
                payload={
                    "journal_id": entry.journal_id,
                    "source_journal_id": source_id,
                    "symbol": entry.symbol,
                    "contract_symbol": entry.contract_symbol,
                    "source_stage": source_stage,
                    "target_hit": bool(outcome.get("target_hit")),
                    "end_return_pct": outcome.get("end_return_pct"),
                    "evaluation_only": True,
                },
            )
        )
        already_labeled.add(source_id)
        labeled += 1
        outcomes.append(outcome)

    return {
        "status": "ok",
        "as_of": observed_at.isoformat(),
        "eligible_source_decisions": eligible,
        "immature": immature,
        "labeled": labeled,
        "skipped": skipped,
        "outcomes": outcomes,
        "config": asdict(cfg),
        "warning": "Outcome rows use future quotes for evaluation only and must never become historical decision features.",
    }


def build_attribution_report(
    journal_rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Summarize observed outcomes by original decision, reasons and surface state."""
    observed: list[dict[str, Any]] = []
    for row in journal_rows:
        if str(row.get("stage") or "") != JournalStage.OUTCOME_OBSERVED.value:
            continue
        outcome = row.get("outcome")
        if not isinstance(outcome, Mapping) or not outcome:
            continue
        observed.append(dict(row))

    if not observed:
        return {
            "samples": 0,
            "by_source_stage": [],
            "reason_attribution": [],
            "surface_attribution": _empty_surface_attribution(),
            "missed_opportunities": 0,
            "avoided_losses": 0,
            "warning": "No mature candidate outcomes are available yet.",
        }

    stage_groups: dict[str, list[Mapping[str, Any]]] = {}
    reason_groups: dict[str, list[Mapping[str, Any]]] = {}
    surface_groups: dict[str, dict[str, list[Mapping[str, Any]]]] = {
        "efficiency_bucket": {},
        "required_move_bucket": {},
        "term_structure": {},
        "skew": {},
        "implied_vs_realized": {},
    }
    missed = 0
    avoided = 0
    for row in observed:
        outcome = _mapping(row.get("outcome"))
        metadata = _mapping(row.get("metadata"))
        stage = str(metadata.get("source_stage") or outcome.get("source_stage") or "unknown")
        stage_groups.setdefault(stage, []).append(outcome)

        if stage == JournalStage.REJECTED.value:
            if _bool(outcome.get("touch_2x")) or _bool(outcome.get("target_hit")):
                missed += 1
            end_return = _number(outcome.get("end_return_pct"))
            if _bool(outcome.get("full_loss_proxy")) or (end_return is not None and end_return <= -50.0):
                avoided += 1

        for reason in _strings(row.get("hard_reasons")) + _strings(row.get("watch_reasons")):
            reason_groups.setdefault(reason, []).append(outcome)

        efficiency_bucket = _surface_efficiency_bucket(row.get("surface_efficiency_score"))
        if efficiency_bucket:
            surface_groups["efficiency_bucket"].setdefault(efficiency_bucket, []).append(outcome)
        move_bucket = _surface_move_bucket(row.get("surface_required_move_ratio"))
        if move_bucket:
            surface_groups["required_move_bucket"].setdefault(move_bucket, []).append(outcome)
        for dimension, field in (
            ("term_structure", "surface_term_structure_state"),
            ("skew", "surface_skew_state"),
            ("implied_vs_realized", "surface_implied_vs_realized_state"),
        ):
            state = _text(row.get(field))
            if state:
                surface_groups[dimension].setdefault(state, []).append(outcome)

    by_stage = [
        {"source_stage": stage, **_outcome_stats(group)}
        for stage, group in sorted(stage_groups.items())
    ]
    reason_attribution = [
        {"reason": reason, **_outcome_stats(group)}
        for reason, group in reason_groups.items()
    ]
    reason_attribution.sort(key=lambda row: (-int(row["samples"]), str(row["reason"])))

    return {
        "samples": len(observed),
        "by_source_stage": by_stage,
        "reason_attribution": reason_attribution,
        "surface_attribution": {
            dimension: [
                {"bucket": bucket, **_outcome_stats(group)}
                for bucket, group in sorted(groups.items())
            ]
            for dimension, groups in surface_groups.items()
        },
        "missed_opportunities": missed,
        "avoided_losses": avoided,
        "overall": _outcome_stats([_mapping(row.get("outcome")) for row in observed]),
        "warning": (
            "Attribution is retrospective evaluation, not a recommendation. Rejected winners and avoided losses must both be retained to evaluate gate quality. Surface buckets are descriptive until sufficient out-of-sample evidence exists."
        ),
    }


def _outcome_entry(
    source: Mapping[str, Any],
    outcome: Mapping[str, Any],
    *,
    identity: SystemIdentity,
) -> JournalEntry:
    source_system = source.get("system")
    system = SystemIdentity.model_validate(source_system) if isinstance(source_system, Mapping) else identity
    environment = _environment(source.get("environment"))
    decision = _decision(source.get("decision"))
    metadata = {
        "source_journal_id": outcome.get("source_journal_id"),
        "source_stage": outcome.get("source_stage"),
        "decision_occurred_at": outcome.get("decision_occurred_at"),
        "evaluation_end": outcome.get("evaluation_end"),
        "evaluated_as_of": outcome.get("evaluated_as_of"),
        "evaluation_only": True,
    }
    return JournalEntry(
        stage=JournalStage.OUTCOME_OBSERVED,
        occurred_at=_timestamp(outcome.get("evaluated_as_of")) or datetime.now(UTC),
        environment=environment,
        system=system,
        snapshot_id=_text(source.get("snapshot_id")),
        source_cycle=_nonnegative_int(source.get("source_cycle")),
        symbol=str(source.get("symbol") or "UNKNOWN").strip().upper(),
        contract_symbol=_text(source.get("contract_symbol")),
        decision=decision,
        displayed=_optional_bool(source.get("displayed")),
        direction=_text(source.get("direction")),
        option_type=_text(source.get("option_type")),
        composite_score=_bounded100(source.get("composite_score")),
        ranking_score=_bounded100(source.get("ranking_score")),
        option_quality_score=_bounded100(source.get("option_quality_score")),
        regime_fit_score=_bounded100(source.get("regime_fit_score")),
        evidence_score=_bounded100(source.get("evidence_score")),
        catalyst_score=_bounded100(source.get("catalyst_score")),
        surface_efficiency_score=_bounded100(source.get("surface_efficiency_score")),
        surface_required_move_ratio=_nonnegative(source.get("surface_required_move_ratio")),
        surface_iv_percentile=_bounded100(source.get("surface_iv_percentile")),
        candidate_iv_premium_to_atm_points=_number(source.get("candidate_iv_premium_to_atm_points")),
        surface_atm_expected_move_pct=_nonnegative(source.get("surface_atm_expected_move_pct")),
        surface_term_structure_state=_text(source.get("surface_term_structure_state")),
        surface_skew_state=_text(source.get("surface_skew_state")),
        surface_implied_vs_realized_state=_text(source.get("surface_implied_vs_realized_state")),
        expected_return_pct=_number(source.get("expected_return_pct")),
        lower_confidence_bound_pct=_number(source.get("lower_confidence_bound_pct")),
        empirical_target_hit_rate=_bounded01(source.get("empirical_target_hit_rate")),
        ev_samples=_nonnegative_int(source.get("ev_samples")),
        entry_ask=_positive(source.get("entry_ask")),
        max_loss_usd_per_contract=_nonnegative(source.get("max_loss_usd_per_contract")),
        quantity=_nonnegative_int(source.get("quantity")),
        hard_reasons=_strings(source.get("hard_reasons")),
        watch_reasons=_strings(source.get("watch_reasons")),
        data_source=_text(source.get("data_source")),
        option_feed=_text(source.get("option_feed")),
        execution_grade_feed=_optional_bool(source.get("execution_grade_feed")),
        outcome=dict(outcome),
        metadata=metadata,
    )


def _outcome_stats(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    values = [dict(row) for row in rows]
    n = len(values)
    if n == 0:
        return {
            "samples": 0,
            "target_hit_rate": 0.0,
            "touch_2x_rate": 0.0,
            "full_loss_proxy_rate": 0.0,
            "mean_end_return_pct": None,
            "median_max_multiple": None,
            "mean_mfe_pct": None,
            "mean_mae_pct": None,
        }
    return {
        "samples": n,
        "target_hit_rate": round(sum(_bool(row.get("target_hit")) for row in values) / n, 4),
        "touch_2x_rate": round(sum(_bool(row.get("touch_2x")) for row in values) / n, 4),
        "full_loss_proxy_rate": round(sum(_bool(row.get("full_loss_proxy")) for row in values) / n, 4),
        "mean_end_return_pct": _mean(values, "end_return_pct"),
        "median_max_multiple": _median(values, "max_multiple"),
        "mean_mfe_pct": _mean(values, "mfe_pct"),
        "mean_mae_pct": _mean(values, "mae_pct"),
    }


def _empty_surface_attribution() -> dict[str, list[dict[str, Any]]]:
    return {
        "efficiency_bucket": [],
        "required_move_bucket": [],
        "term_structure": [],
        "skew": [],
        "implied_vs_realized": [],
    }


def _surface_efficiency_bucket(value: object) -> str | None:
    number = _number(value)
    if number is None:
        return None
    if number < 35.0:
        return "weak_<35"
    if number < 60.0:
        return "middle_35_60"
    return "strong_>=60"


def _surface_move_bucket(value: object) -> str | None:
    number = _number(value)
    if number is None or number < 0:
        return None
    if number < 1.0:
        return "<1.0x"
    if number < 1.5:
        return "1.0_1.5x"
    if number <= 2.0:
        return "1.5_2.0x"
    return ">2.0x"


def _evaluation_end(contract: str, occurred_at: datetime, horizon_days: int) -> datetime | None:
    match = _OCC_RE.fullmatch(contract)
    if not match:
        return None
    try:
        expiration = datetime.strptime(match.group(2), "%y%m%d").date()
    except ValueError:
        return None
    horizon_end = occurred_at + timedelta(days=horizon_days)
    expiry_end = datetime.combine(expiration, time(23, 59, 59), tzinfo=UTC)
    return min(horizon_end, expiry_end)


def _environment(value: object) -> PlatformEnvironment:
    try:
        return PlatformEnvironment(str(value or "research"))
    except ValueError:
        return PlatformEnvironment.RESEARCH


def _decision(value: object) -> DeskDecision | None:
    if value is None:
        return None
    try:
        return DeskDecision(str(value))
    except ValueError:
        return None


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _positive(value: object) -> float | None:
    number = _number(value)
    return number if number is not None and number > 0 else None


def _nonnegative(value: object) -> float | None:
    number = _number(value)
    return number if number is not None and number >= 0 else None


def _bounded100(value: object) -> float | None:
    number = _number(value)
    return None if number is None else max(0.0, min(100.0, number))


def _bounded01(value: object) -> float | None:
    number = _number(value)
    return None if number is None else max(0.0, min(1.0, number))


def _nonnegative_int(value: object) -> int | None:
    number = _number(value)
    if number is None or number < 0 or not float(number).is_integer():
        return None
    return int(number)


def _optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    token = str(value).strip().lower()
    if token in {"true", "1", "yes"}:
        return True
    if token in {"false", "0", "no"}:
        return False
    return None


def _bool(value: object) -> bool:
    return bool(_optional_bool(value))


def _mean(rows: list[Mapping[str, Any]], field: str) -> float | None:
    values = [_number(row.get(field)) for row in rows]
    clean = [value for value in values if value is not None]
    return None if not clean else round(sum(clean) / len(clean), 4)


def _median(rows: list[Mapping[str, Any]], field: str) -> float | None:
    values = sorted(value for row in rows if (value := _number(row.get(field))) is not None)
    if not values:
        return None
    midpoint = len(values) // 2
    if len(values) % 2:
        return round(values[midpoint], 4)
    return round((values[midpoint - 1] + values[midpoint]) / 2.0, 4)


__all__ = ["AttributionConfig", "build_attribution_report", "checkpoint_candidate_outcomes"]
