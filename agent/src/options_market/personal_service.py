"""Composition layer: continuous market research -> personal decision dashboard.

This wrapper keeps the continuous whole-market scanner reusable while adding
personal-account evidence/risk gates and meaningful-change alerting. It remains
broker-write free; an account provider may perform read-only account/position
reads, but no order API is referenced here. The result reports whether a
candidate is eligible to enter the separate supervised paper lifecycle instead
of the obsolete hard-coded ``execution: none`` marker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from .continuous import ContinuousOptionsAnalyzer, TradingSession
from .dashboard import build_alert_event, build_personal_dashboard
from .personal import PersonalDecisionConfig, build_personal_shortlist
from .risk import PortfolioRiskConfig


@dataclass(frozen=True)
class PersonalAccountState:
    equity_usd: float
    option_risk_positions: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class PersonalEvidence:
    ev_reports: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    walk_forward_reports: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    regime_report: Mapping[str, Any] = field(default_factory=dict)
    realized_vol_by_symbol: Mapping[str, float] = field(default_factory=dict)
    iv_percentile_by_contract: Mapping[str, float] = field(default_factory=dict)


AccountStateProvider = Callable[[datetime], PersonalAccountState]
EvidenceProvider = Callable[[Sequence[Mapping[str, Any]], datetime], PersonalEvidence]
DashboardLoad = Callable[[], Mapping[str, Any] | None]
DashboardSave = Callable[[Mapping[str, Any]], None]
CyclePublisher = Callable[[Mapping[str, Any]], None]


class PersonalContinuousOptionsService:
    """Apply personal research gates after every continuous market-analysis cycle."""

    def __init__(
        self,
        *,
        analyzer: ContinuousOptionsAnalyzer,
        account_provider: AccountStateProvider,
        evidence_provider: EvidenceProvider,
        decision_config: PersonalDecisionConfig | None = None,
        risk_config: PortfolioRiskConfig | None = None,
        load_previous_dashboard: DashboardLoad | None = None,
        save_dashboard: DashboardSave | None = None,
        publish_cycle: CyclePublisher | None = None,
    ) -> None:
        self.analyzer = analyzer
        self.account_provider = account_provider
        self.evidence_provider = evidence_provider
        self.decision_config = decision_config or PersonalDecisionConfig()
        self.risk_config = risk_config
        self.load_previous_dashboard = load_previous_dashboard
        self.save_dashboard = save_dashboard
        self.publish_cycle = publish_cycle

    def run_cycle(
        self,
        *,
        now: datetime,
        session: TradingSession,
        force_full: bool = False,
        force_focus: bool = False,
    ) -> dict[str, Any]:
        """Run one market cycle and return market + personal + dashboard results."""
        market = self.analyzer.run_cycle(
            now=now,
            session=session,
            force_full=force_full,
            force_focus=force_focus,
        )
        candidates = _latest_candidates(market)
        account = self.account_provider(now)
        evidence = self.evidence_provider(candidates, now)
        personal = build_personal_shortlist(
            candidates,
            account_equity_usd=account.equity_usd,
            existing_positions=account.option_risk_positions,
            ev_reports=evidence.ev_reports,
            walk_forward_reports=evidence.walk_forward_reports,
            regime_report=evidence.regime_report,
            realized_vol_by_symbol=evidence.realized_vol_by_symbol,
            iv_percentile_by_contract=evidence.iv_percentile_by_contract,
            config=self.decision_config,
            risk_config=self.risk_config,
        )
        dashboard = build_personal_dashboard(personal, funnel=_funnel(market))
        previous = self.load_previous_dashboard() if self.load_previous_dashboard else None
        alert = build_alert_event(dashboard, previous)
        if self.save_dashboard:
            self.save_dashboard(dashboard)
        result = {
            "mode": "continuous_personal_options_research",
            "market": market,
            "personal": personal,
            "dashboard": dashboard,
            "alert": alert,
            "execution": build_execution_readiness(personal),
        }
        if self.publish_cycle:
            self.publish_cycle(result)
        return result


def build_execution_readiness(personal: Mapping[str, Any]) -> dict[str, Any]:
    """Describe eligibility for the separate supervised paper lifecycle.

    This is intentionally not an order. It removes the misleading historical
    ``execution: none`` field while retaining the approval boundary: a candidate
    must first be ``TRADE_READY_RESEARCH`` and the paper runtime must still
    re-check market clock, exact-contract quote freshness, OPRA, account risk and
    paper profile before any broker mutation.
    """
    rows = personal.get("candidates") if isinstance(personal.get("candidates"), list) else []
    ready = [
        dict(row)
        for row in rows
        if isinstance(row, Mapping) and row.get("decision") == "TRADE_READY_RESEARCH"
    ]
    contracts = [
        str(row.get("contract_symbol") or "").strip().upper()
        for row in ready
        if str(row.get("contract_symbol") or "").strip()
    ]
    if ready:
        status = "PAPER_RUNTIME_CHECK_REQUIRED"
        reasons: list[str] = []
        next_action = "reprice_and_validate_with_supervised_paper_lifecycle"
    else:
        status = "BLOCKED_BY_RESEARCH_GATES"
        reasons = ["no_TRADE_READY_RESEARCH_candidate"]
        next_action = "continue_research_and_evidence_refresh"
    return {
        "mode": "supervised_paper",
        "status": status,
        "eligible_candidates": len(ready),
        "eligible_contracts": contracts,
        "automatic_submission": False,
        "paper_submit_confirmation_required": True,
        "runtime_revalidation_required": True,
        "reasons": reasons,
        "next_action": next_action,
    }


def _latest_candidates(market: Mapping[str, Any]) -> list[dict[str, Any]]:
    final_stage = market.get("final_stage")
    if isinstance(final_stage, Mapping) and isinstance(final_stage.get("candidates"), list):
        return [dict(row) for row in final_stage["candidates"] if isinstance(row, Mapping)]
    state = market.get("state")
    if isinstance(state, Mapping) and isinstance(state.get("latest_shortlist"), list):
        return [dict(row) for row in state["latest_shortlist"] if isinstance(row, Mapping)]
    return []


def _funnel(market: Mapping[str, Any]) -> dict[str, int]:
    chart = market.get("chart_stage") if isinstance(market.get("chart_stage"), Mapping) else {}
    final = market.get("final_stage") if isinstance(market.get("final_stage"), Mapping) else {}
    state = market.get("state") if isinstance(market.get("state"), Mapping) else {}
    latest = state.get("latest_shortlist") if isinstance(state.get("latest_shortlist"), list) else []
    return {
        "universe": int(chart.get("universe_received") or chart.get("universe_requested") or 0),
        "chart_eligible": int(chart.get("eligible_for_cross_section") or 0),
        "chart_candidates": int(chart.get("candidate_count") or len(state.get("chart_candidates") or [])),
        "final_candidates": int(final.get("candidate_count") or len(latest)),
    }


__all__ = [
    "CyclePublisher",
    "PersonalAccountState",
    "PersonalContinuousOptionsService",
    "PersonalEvidence",
    "build_execution_readiness",
]
