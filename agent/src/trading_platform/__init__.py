"""Personal trading-platform foundation."""

from .health import DEFAULT_FRESHNESS_POLICIES, FreshnessPolicy, evaluate_data_freshness
from .models import (
    ComponentHealth,
    DataQualitySummary,
    DeskDecision,
    ExecutionMode,
    FunnelCounts,
    HealthStatus,
    OpportunityCard,
    PlatformEnvironment,
    PlatformEvent,
    RiskSummary,
    SystemIdentity,
    TradingDeskSnapshot,
)
from .service import TradingPlatformService
from .store import TradingPlatformStore

__all__ = [
    "ComponentHealth",
    "DEFAULT_FRESHNESS_POLICIES",
    "DataQualitySummary",
    "DeskDecision",
    "ExecutionMode",
    "FreshnessPolicy",
    "FunnelCounts",
    "HealthStatus",
    "OpportunityCard",
    "PlatformEnvironment",
    "PlatformEvent",
    "RiskSummary",
    "SystemIdentity",
    "TradingDeskSnapshot",
    "TradingPlatformService",
    "TradingPlatformStore",
    "evaluate_data_freshness",
]
