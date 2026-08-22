"""Compose personal-options research cycles into durable Trading Desk snapshots."""

from __future__ import annotations

from typing import Any, Mapping

from .journal import JournalEntry, stage_for_decision
from .models import (
    DataQualitySummary,
    DeskDecision,
    ExecutionMode,
    FunnelCounts,
    OpportunityCard,
    PlatformEnvironment,
    PlatformEvent,
    RiskSummary,
    SystemIdentity,
    TradingDeskSnapshot,
)
from .store import TradingPlatformStore


class TradingPlatformService:
    """Publish read-only platform state, audit events and candidate decisions.

    This service deliberately stops at research state. Even in ``paper`` mode it
    only reports that explicit approval is required; it never imports a broker
    order function.
    """

    def __init__(
        self,
        *,
        store: TradingPlatformStore,
        environment: PlatformEnvironment = PlatformEnvironment.RESEARCH,
        system: SystemIdentity | None = None,
    ) -> None:
        self.store = store
        self.environment = environment
        self.system = system or SystemIdentity()

    def publish_personal_cycle(
        self,
        cycle: Mapping[str, Any],
        *,
        data_quality: DataQualitySummary | None = None,
        risk: RiskSummary | None = None,
    ) -> TradingDeskSnapshot:
        market = _mapping(cycle.get("market"))
        personal = _mapping(cycle.get("personal"))
        dashboard = _mapping(cycle.get("dashboard"))
        state = _mapping(market.get("state"))
        plan = _mapping(market.get("plan"))
        risk_summary = risk or RiskSummary()
        funnel = _funnel(dashboard.get("funnel"))
        decision = _decision(dashboard.get("decision") or personal.get("decision"))
        headline = str(dashboard.get("headline") or "Trading research snapshot")

        # Portfolio/account risk is a top-level veto, not another ranking feature.
        # Preserve opportunity cards for research/audit, but never let a candidate
        # appear account-approved when the account itself is blocked.
        if risk_summary.trading_blocked:
            decision = DeskDecision.NO_TRADE
            funnel = funnel.model_copy(update={"trade_ready": 0})
            headline = "NO TRADE — account risk gate blocked additional exposure"

        snapshot = TradingDeskSnapshot(
            environment=self.environment,
            execution_mode=_execution_mode(self.environment),
            decision=decision,
            headline=headline,
            system=self.system,
            market_phase=str(plan.get("phase") or state.get("latest_phase") or "unknown"),
            market_regime=str(dashboard.get("market_regime") or "unknown"),
            market_regime_confidence=_bounded01(dashboard.get("market_regime_confidence")),
            funnel=funnel,
            opportunities=_opportunities(dashboard.get("cards")),
            risk=risk_summary,
            data_quality=data_quality or DataQualitySummary(),
            source_cycle=_nonnegative_int(state.get("cycle")),
            warnings=_warnings(cycle, dashboard),
        )
        self.store.save_snapshot(snapshot)
        self.store.append_event(
            PlatformEvent(
                event_type="desk_snapshot_published",
                environment=self.environment,
                system=self.system,
                payload={
                    "snapshot_id": snapshot.snapshot_id,
                    "decision": snapshot.decision.value,
                    "market_phase": snapshot.market_phase,
                    "market_regime": snapshot.market_regime,
                    "opportunity_count": len(snapshot.opportunities),
                    "data_healthy": snapshot.data_quality.healthy,
                    "execution_mode": snapshot.execution_mode.value,
                    "account_risk_blocked": snapshot.risk.trading_blocked,
                    "account_risk_reasons": list(snapshot.risk.blocking_reasons),
                    "paper_execution_status": _execution_status(cycle),
                },
            )
        )

        journal_rows = personal.get("journal_candidates")
        if not isinstance(journal_rows, list):
            # Backward-compatible fallback for cycles created before the full
            # evaluated-candidate journal payload existed.
            journal_rows = personal.get("candidates") if isinstance(personal.get("candidates"), list) else []
        journal_count = 0
        for raw in journal_rows:
            if not isinstance(raw, Mapping):
                continue
            entry = _journal_entry(raw, snapshot=snapshot)
            if entry is None:
                continue
            self.store.append_journal_entry(entry)
            journal_count += 1
        if journal_count:
            self.store.append_event(
                PlatformEvent(
                    event_type="candidate_batch_journaled",
                    environment=self.environment,
                    system=self.system,
                    payload={
                        "snapshot_id": snapshot.snapshot_id,
                        "candidate_count": journal_count,
                    },
                )
            )

        alert = _mapping(cycle.get("alert"))
        if bool(alert.get("should_alert")):
            self.store.append_event(
                PlatformEvent(
                    event_type="research_alert",
                    environment=self.environment,
                    system=self.system,
                    payload=dict(alert),
                )
            )
        return snapshot

    def latest_desk(self) -> dict[str, Any]:
        snapshot = self.store.latest_snapshot()
        return {
            "status": "ok",
            "snapshot": snapshot.model_dump(mode="json") if snapshot else None,
            "counts": self.store.counts(),
        }


def _journal_entry(raw: Mapping[str, Any], *, snapshot: TradingDeskSnapshot) -> JournalEntry | None:
    symbol = str(raw.get("symbol") or "").strip().upper()
    if not symbol:
        return None
    decision = _decision(raw.get("decision"))
    decision_value: DeskDecision | None = None if decision is DeskDecision.NO_DATA else decision
    return JournalEntry(
        stage=stage_for_decision(decision_value),
        environment=snapshot.environment,
        system=snapshot.system,
        snapshot_id=snapshot.snapshot_id,
        source_cycle=snapshot.source_cycle,
        symbol=symbol,
        contract_symbol=_text(raw.get("contract_symbol")),
        decision=decision_value,
        displayed=bool(raw.get("displayed")) if raw.get("displayed") is not None else None,
        direction=_text(raw.get("direction")),
        option_type=_text(raw.get("option_type")),
        composite_score=_bounded100(raw.get("composite_score")),
        ranking_score=_bounded100(raw.get("ranking_score")),
        option_quality_score=_bounded100(raw.get("option_quality_score")),
        regime_fit_score=_bounded100(raw.get("regime_fit_score")),
        evidence_score=_bounded100(raw.get("evidence_score")),
        catalyst_score=_bounded100(raw.get("catalyst_score")),
        expected_return_pct=_finite(raw.get("expected_return_pct")),
        lower_confidence_bound_pct=_finite(raw.get("lower_confidence_bound_pct")),
        empirical_target_hit_rate=_bounded01(raw.get("empirical_target_hit_rate")),
        ev_samples=_nonnegative_int(raw.get("ev_samples")),
        entry_ask=_nonnegative(raw.get("entry_ask")),
        max_loss_usd_per_contract=_nonnegative(raw.get("max_loss_usd_per_contract")),
        quantity=_nonnegative_int(raw.get("quantity")),
        hard_reasons=_strings(raw.get("hard_reasons")),
        watch_reasons=_strings(raw.get("watch_reasons")),
        data_source=_text(raw.get("data_source")),
        option_feed=_text(raw.get("option_feed")),
        execution_grade_feed=(bool(raw.get("execution_grade_feed")) if raw.get("execution_grade_feed") is not None else None),
    )


def _execution_mode(environment: PlatformEnvironment) -> ExecutionMode:
    if environment is PlatformEnvironment.PAPER:
        return ExecutionMode.PAPER_APPROVAL_REQUIRED
    if environment is PlatformEnvironment.LIVE:
        return ExecutionMode.LIVE_DISABLED
    return ExecutionMode.RESEARCH_ONLY


def _execution_status(cycle: Mapping[str, Any]) -> str | None:
    execution = cycle.get("execution")
    if isinstance(execution, Mapping):
        mode = str(execution.get("mode") or "").strip().lower()
        status = str(execution.get("status") or "").strip()
        return f"{mode}:{status}" if mode and status else mode or status or None
    token = str(execution or "").strip()
    return token or None


def _decision(value: object) -> DeskDecision:
    token = str(value or "NO_DATA").strip().upper()
    try:
        return DeskDecision(token)
    except ValueError:
        return DeskDecision.NO_DATA


def _funnel(value: object) -> FunnelCounts:
    raw = _mapping(value)
    return FunnelCounts(
        universe=_count(raw.get("universe")),
        chart_eligible=_count(raw.get("chart_eligible")),
        chart_candidates=_count(raw.get("chart_candidates")),
        deep_analyzed=_count(raw.get("deep_analyzed") or raw.get("final_candidates")),
        positive_ev=_count(raw.get("positive_ev")),
        risk_approved=_count(raw.get("risk_approved")),
        trade_ready=_count(raw.get("trade_ready")),
    )


def _opportunities(value: object) -> list[OpportunityCard]:
    rows = value if isinstance(value, list) else []
    output: list[OpportunityCard] = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        symbol = str(raw.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        output.append(
            OpportunityCard(
                symbol=symbol,
                contract_symbol=_text(raw.get("contract_symbol")),
                decision=_decision(raw.get("decision")),
                direction=_text(raw.get("direction")),
                composite_score=_bounded100(raw.get("composite_score")),
                ranking_score=_bounded100(raw.get("ranking_score")),
                option_quality_score=_bounded100(raw.get("option_quality_score")),
                regime_fit_score=_bounded100(raw.get("regime_fit_score")),
                expected_return_pct=_finite(raw.get("expected_return_pct")),
                lower_confidence_bound_pct=_finite(raw.get("lower_confidence_bound_pct")),
                historical_samples=_nonnegative_int(raw.get("historical_samples")),
                historical_target_hit_rate=_bounded01(raw.get("historical_target_hit_rate")),
                entry_ask=_nonnegative(raw.get("entry_ask")),
                max_loss_usd_per_contract=_nonnegative(raw.get("max_loss_usd_per_contract")),
                configured_contract_cap=_nonnegative_int(raw.get("configured_contract_cap")),
                spread_pct=_nonnegative(raw.get("spread_pct")),
                iv_percentile=_bounded100(raw.get("iv_percentile")),
                data_feed=_text(raw.get("data_feed")),
                hard_reasons=_strings(raw.get("hard_reasons")),
                watch_reasons=_strings(raw.get("watch_reasons")),
            )
        )
    return output


def _warnings(cycle: Mapping[str, Any], dashboard: Mapping[str, Any]) -> list[str]:
    warnings = _strings(cycle.get("warnings"))
    disclaimer = _text(dashboard.get("disclaimer"))
    if disclaimer:
        warnings.append(disclaimer)

    execution = cycle.get("execution")
    if isinstance(execution, Mapping):
        mode = str(execution.get("mode") or "").strip().lower()
        if mode and mode != "supervised_paper":
            warnings.append(f"Unknown execution mode observed: {mode}; TradingPlatformService did not execute it.")
    elif execution not in (None, "", "none"):
        warnings.append("Legacy/unknown execution marker observed; TradingPlatformService did not execute it.")
    return list(dict.fromkeys(warnings))


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _finite(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _nonnegative(value: object) -> float | None:
    number = _finite(value)
    return number if number is not None and number >= 0 else None


def _bounded100(value: object) -> float | None:
    number = _finite(value)
    return None if number is None else min(100.0, max(0.0, number))


def _bounded01(value: object) -> float | None:
    number = _finite(value)
    return None if number is None else min(1.0, max(0.0, number))


def _count(value: object) -> int:
    number = _nonnegative_int(value)
    return 0 if number is None else number


def _nonnegative_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number >= 0 else None


__all__ = ["TradingPlatformService"]
