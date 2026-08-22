"""Append-only candidate and trade lifecycle contracts.

The journal records what the platform decided and why. Recording a lifecycle
state never grants authority to execute it; broker mutation remains behind the
separate mandate/order boundary.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import DeskDecision, PlatformEnvironment, SystemIdentity


class JournalStage(str, Enum):
    EVALUATED = "evaluated"
    REJECTED = "rejected"
    WATCH = "watch"
    TRADE_READY = "trade_ready"
    PROPOSED = "proposed"
    APPROVED = "approved"
    SUBMITTED = "submitted"
    PARTIAL_FILL = "partial_fill"
    FILLED = "filled"
    EXIT_PROPOSED = "exit_proposed"
    EXITED = "exited"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    ERROR = "error"


class JournalEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    journal_id: str = Field(default_factory=lambda: uuid4().hex)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    stage: JournalStage
    environment: PlatformEnvironment
    system: SystemIdentity
    snapshot_id: str | None = None
    source_cycle: int | None = Field(default=None, ge=0)
    symbol: str
    contract_symbol: str | None = None
    decision: DeskDecision | None = None
    displayed: bool | None = None
    direction: str | None = None
    option_type: str | None = None
    composite_score: float | None = Field(default=None, ge=0, le=100)
    ranking_score: float | None = Field(default=None, ge=0, le=100)
    option_quality_score: float | None = Field(default=None, ge=0, le=100)
    regime_fit_score: float | None = Field(default=None, ge=0, le=100)
    evidence_score: float | None = Field(default=None, ge=0, le=100)
    catalyst_score: float | None = Field(default=None, ge=0, le=100)
    expected_return_pct: float | None = None
    lower_confidence_bound_pct: float | None = None
    empirical_target_hit_rate: float | None = Field(default=None, ge=0, le=1)
    ev_samples: int | None = Field(default=None, ge=0)
    entry_ask: float | None = Field(default=None, ge=0)
    max_loss_usd_per_contract: float | None = Field(default=None, ge=0)
    quantity: int | None = Field(default=None, ge=0)
    hard_reasons: list[str] = Field(default_factory=list)
    watch_reasons: list[str] = Field(default_factory=list)
    data_source: str | None = None
    option_feed: str | None = None
    execution_grade_feed: bool | None = None
    broker_order_id: str | None = None
    fill_price: float | None = Field(default=None, ge=0)
    realized_pnl_usd: float | None = None
    outcome: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def _aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")
        return value

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, value: str) -> str:
        clean = value.strip().upper()
        if not clean:
            raise ValueError("symbol is required")
        return clean

    @field_validator("contract_symbol")
    @classmethod
    def _contract(cls, value: str | None) -> str | None:
        if value is None:
            return None
        clean = value.strip().upper()
        return clean or None


def stage_for_decision(decision: DeskDecision | str | None) -> JournalStage:
    token = str(getattr(decision, "value", decision) or "").upper()
    if token == DeskDecision.TRADE_READY_RESEARCH.value:
        return JournalStage.TRADE_READY
    if token == DeskDecision.WATCH.value:
        return JournalStage.WATCH
    if token in {DeskDecision.PASS.value, DeskDecision.NO_TRADE.value}:
        return JournalStage.REJECTED
    return JournalStage.EVALUATED


__all__ = ["JournalEntry", "JournalStage", "stage_for_decision"]
