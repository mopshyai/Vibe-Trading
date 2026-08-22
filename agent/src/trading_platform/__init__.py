"""Personal trading-platform foundation."""

from .data_manifest import DataPlaneManifest
from .exit_manager import ExitPolicyConfig, evaluate_long_option_exit, scan_long_option_exits
from .health import DEFAULT_FRESHNESS_POLICIES, FreshnessPolicy, evaluate_data_freshness
from .journal import JournalEntry, JournalStage, stage_for_decision
from .market_calendar import (
    MarketCalendarDependencyError,
    xnys_session_calendar,
    xnys_trading_session,
)
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
from .paper_lifecycle import (
    PaperLifecycleConfig,
    append_reconciled_order_status,
    fetch_alpaca_market_clock,
    fetch_alpaca_order,
    fetch_current_option_quote,
    run_paper_option_lifecycle,
    sync_paper_order_lifecycle,
)
from .portfolio_risk import (
    AccountRiskConfig,
    assess_account_portfolio_risk,
    risk_summary_from_report,
)
from .preflight import run_platform_preflight
from .service import TradingPlatformService
from .store import TradingPlatformStore

__all__ = [
    "AccountRiskConfig",
    "ComponentHealth",
    "DEFAULT_FRESHNESS_POLICIES",
    "DataPlaneManifest",
    "DataQualitySummary",
    "DeskDecision",
    "ExecutionMode",
    "ExitPolicyConfig",
    "FreshnessPolicy",
    "FunnelCounts",
    "HealthStatus",
    "JournalEntry",
    "JournalStage",
    "MarketCalendarDependencyError",
    "OpportunityCard",
    "PaperLifecycleConfig",
    "PlatformEnvironment",
    "PlatformEvent",
    "RiskSummary",
    "SystemIdentity",
    "TradingDeskSnapshot",
    "TradingPlatformService",
    "TradingPlatformStore",
    "append_reconciled_order_status",
    "assess_account_portfolio_risk",
    "evaluate_data_freshness",
    "evaluate_long_option_exit",
    "fetch_alpaca_market_clock",
    "fetch_alpaca_order",
    "fetch_current_option_quote",
    "risk_summary_from_report",
    "run_paper_option_lifecycle",
    "run_platform_preflight",
    "scan_long_option_exits",
    "stage_for_decision",
    "sync_paper_order_lifecycle",
    "xnys_session_calendar",
    "xnys_trading_session",
]
