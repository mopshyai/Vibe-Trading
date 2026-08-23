"""Versioned contracts shared by the personal trading platform.

These models are intentionally broker-neutral. They describe what the research,
risk, health and UI layers know at a point in time without granting any authority
to place, cancel or modify an order.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


SCHEMA_VERSION = 1


class PlatformEnvironment(str, Enum):
    RESEARCH = "research"
    PAPER = "paper"
    LIVE = "live"


class DeskDecision(str, Enum):
    TRADE_READY_RESEARCH = "TRADE_READY_RESEARCH"
    WATCH = "WATCH"
    PASS = "PASS"
    NO_TRADE = "NO_TRADE"
    NO_DATA = "NO_DATA"


class HealthStatus(str, Enum):
    OK = "ok"
    STALE = "stale"
    ERROR = "error"
    UNKNOWN = "unknown"


class ExecutionMode(str, Enum):
    RESEARCH_ONLY = "research_only"
    PAPER_APPROVAL_REQUIRED = "paper_approval_required"
    LIVE_DISABLED = "live_disabled"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SystemIdentity(StrictModel):
    platform_version: str = "personal-trading-platform-v0.1"
    strategy_version: str = "personal-options-v0.1"
    model_version: str = "rules-and-empirical-v0.1"
    risk_policy_version: str = "personal-risk-v0.1"
    payoff_policy_version: str = "adaptive-payoff-v0.1"
    commit_sha: str | None = None


class ComponentHealth(StrictModel):
    name: str
    status: HealthStatus = HealthStatus.UNKNOWN
    observed_at: datetime | None = None
    age_seconds: float | None = Field(default=None, ge=0)
    source: str | None = None
    detail: str | None = None
    blocking: bool = False

    @field_validator("observed_at")
    @classmethod
    def _aware_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        return value


class DataQualitySummary(StrictModel):
    healthy: bool = False
    components: list[ComponentHealth] = Field(default_factory=list)
    blocking_reasons: list[str] = Field(default_factory=list)

    @classmethod
    def from_components(cls, components: list[ComponentHealth]) -> "DataQualitySummary":
        reasons = [
            f"{component.name}:{component.status.value}"
            for component in components
            if component.blocking and component.status is not HealthStatus.OK
        ]
        return cls(
            healthy=not reasons and all(c.status is not HealthStatus.ERROR for c in components),
            components=components,
            blocking_reasons=reasons,
        )


class FunnelCounts(StrictModel):
    universe: int = Field(default=0, ge=0)
    chart_eligible: int = Field(default=0, ge=0)
    chart_candidates: int = Field(default=0, ge=0)
    deep_analyzed: int = Field(default=0, ge=0)
    positive_ev: int = Field(default=0, ge=0)
    risk_approved: int = Field(default=0, ge=0)
    trade_ready: int = Field(default=0, ge=0)


class RiskSummary(StrictModel):
    account_equity_usd: float | None = Field(default=None, gt=0)
    open_premium_risk_usd: float | None = Field(default=None, ge=0)
    open_premium_risk_pct: float | None = Field(default=None, ge=0)
    daily_realized_pnl_usd: float | None = None
    weekly_realized_pnl_usd: float | None = None
    max_drawdown_pct: float | None = Field(default=None, ge=0)
    positions: int = Field(default=0, ge=0)
    trading_blocked: bool = False
    blocking_reasons: list[str] = Field(default_factory=list)
    greeks: dict[str, Any] = Field(default_factory=dict)
    concentration: dict[str, Any] = Field(default_factory=dict)


class OpportunityCard(StrictModel):
    symbol: str
    contract_symbol: str | None = None
    decision: DeskDecision
    direction: str | None = None
    composite_score: float | None = Field(default=None, ge=0, le=100)
    ranking_score: float | None = Field(default=None, ge=0, le=100)
    option_quality_score: float | None = Field(default=None, ge=0, le=100)
    regime_fit_score: float | None = Field(default=None, ge=0, le=100)
    expected_return_pct: float | None = None
    lower_confidence_bound_pct: float | None = None
    historical_samples: int | None = Field(default=None, ge=0)
    historical_target_hit_rate: float | None = Field(default=None, ge=0, le=1)
    entry_ask: float | None = Field(default=None, ge=0)
    max_loss_usd_per_contract: float | None = Field(default=None, ge=0)
    configured_contract_cap: int | None = Field(default=None, ge=0)
    spread_pct: float | None = Field(default=None, ge=0)
    iv_percentile: float | None = Field(default=None, ge=0, le=100)
    surface_efficiency_score: float | None = Field(default=None, ge=0, le=100)
    surface_required_move_ratio: float | None = Field(default=None, ge=0)
    candidate_iv_premium_to_atm_points: float | None = None
    surface_atm_expected_move_pct: float | None = Field(default=None, ge=0)
    surface_term_structure_state: str | None = None
    surface_skew_state: str | None = None
    surface_implied_vs_realized_state: str | None = None
    data_feed: str | None = None
    hard_reasons: list[str] = Field(default_factory=list)
    watch_reasons: list[str] = Field(default_factory=list)


class TradingDeskSnapshot(StrictModel):
    schema_version: int = SCHEMA_VERSION
    snapshot_id: str = Field(default_factory=lambda: uuid4().hex)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    environment: PlatformEnvironment = PlatformEnvironment.RESEARCH
    execution_mode: ExecutionMode = ExecutionMode.RESEARCH_ONLY
    decision: DeskDecision = DeskDecision.NO_DATA
    headline: str = "No trading-platform snapshot has been published yet"
    system: SystemIdentity = Field(default_factory=SystemIdentity)
    market_phase: str = "unknown"
    market_regime: str = "unknown"
    market_regime_confidence: float | None = Field(default=None, ge=0, le=1)
    funnel: FunnelCounts = Field(default_factory=FunnelCounts)
    opportunities: list[OpportunityCard] = Field(default_factory=list)
    risk: RiskSummary = Field(default_factory=RiskSummary)
    data_quality: DataQualitySummary = Field(default_factory=DataQualitySummary)
    source_cycle: int | None = Field(default=None, ge=0)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("created_at")
    @classmethod
    def _created_at_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        return value


class PlatformEvent(StrictModel):
    event_id: str = Field(default_factory=lambda: uuid4().hex)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    event_type: str
    environment: PlatformEnvironment
    system: SystemIdentity
    payload: dict[str, Any] = Field(default_factory=dict)


def normalize_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return dict(value or {})


__all__ = [
    "ComponentHealth",
    "DataQualitySummary",
    "DeskDecision",
    "ExecutionMode",
    "FunnelCounts",
    "HealthStatus",
    "OpportunityCard",
    "PlatformEnvironment",
    "PlatformEvent",
    "RiskSummary",
    "SCHEMA_VERSION",
    "SystemIdentity",
    "TradingDeskSnapshot",
]
