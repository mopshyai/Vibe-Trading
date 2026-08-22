"""Continuous, provider-independent orchestration for U.S. options research.

The module turns the batch market scanner into a repeatable service while keeping
broker mutation out of the analysis loop.  It deliberately separates:

* market-session timing,
* point-in-time equity research,
* expensive focus-list option/catalyst refreshes, and
* persisted checkpoints.

A caller can force the same pipeline on demand (for example when a user asks
"analyze now") or run it continuously.  Data providers are injected so the
strategy layer does not depend on Databento, Yahoo, Alpaca, or any other vendor.

No function in this module places, cancels, or modifies broker orders.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable, Mapping, Protocol, Sequence
from zoneinfo import ZoneInfo

from .chart_screen import ChartScreenConfig, scan_market_frames
from .pipeline import OptionsMarketPipelineConfig, combine_rankings
from .store import OptionsResearchStore

_ET = ZoneInfo("America/New_York")
_UTC = timezone.utc


class MarketPhase(str, Enum):
    """Coarse U.S. regular-session phases used to choose analysis cadence."""

    CLOSED = "closed"
    PREMARKET = "premarket"
    OPENING = "opening"
    REGULAR = "regular"
    POWER_HOUR = "power_hour"
    POSTMARKET = "postmarket"


@dataclass(frozen=True)
class TradingSession:
    """One authoritative exchange session.

    ``open_at`` and ``close_at`` must be timezone-aware.  Production callers
    should obtain these timestamps from an exchange calendar or market-data
    provider so holidays and early closes are represented correctly.
    """

    open_at: datetime
    close_at: datetime
    trading_day: bool = True
    source: str = "caller"
    authoritative: bool = True

    def validate(self) -> None:
        if self.open_at.tzinfo is None or self.close_at.tzinfo is None:
            raise ValueError("session open_at/close_at must be timezone-aware")
        if self.close_at <= self.open_at:
            raise ValueError("session close_at must be after open_at")


@dataclass(frozen=True)
class ContinuousScanConfig:
    """Cadence and breadth controls for the continuous research loop."""

    history_days: int = 420
    max_focus_symbols: int = 200
    max_deep_symbols: int = 50
    final_top_n: int = 10
    target_profit_pct: float = 300.0
    premarket_minutes: int = 330  # 04:00 ET for a normal 09:30 open
    opening_minutes: int = 30
    power_hour_minutes: int = 60
    postmarket_minutes: int = 240  # through 20:00 ET for a normal session
    closed_full_scan_minutes: int = 360
    premarket_full_scan_minutes: int = 60
    opening_full_scan_minutes: int = 15
    regular_full_scan_minutes: int = 60
    power_hour_full_scan_minutes: int = 30
    postmarket_full_scan_minutes: int = 60
    premarket_focus_minutes: int = 15
    opening_focus_minutes: int = 2
    regular_focus_minutes: int = 5
    power_hour_focus_minutes: int = 2
    postmarket_focus_minutes: int = 15
    min_cycle_sleep_seconds: int = 15
    max_cycle_sleep_seconds: int = 300

    def validate(self) -> None:
        if self.history_days < 220:
            raise ValueError("history_days must be at least 220 for the chart scanner")
        if not 1 <= self.max_deep_symbols <= self.max_focus_symbols:
            raise ValueError("max_deep_symbols must be between 1 and max_focus_symbols")
        if not 1 <= self.final_top_n <= self.max_deep_symbols:
            raise ValueError("final_top_n must be between 1 and max_deep_symbols")
        if self.target_profit_pct <= 0:
            raise ValueError("target_profit_pct must be positive")
        for name, value in asdict(self).items():
            if name.endswith("_minutes") and int(value) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.min_cycle_sleep_seconds <= 0:
            raise ValueError("min_cycle_sleep_seconds must be positive")
        if self.max_cycle_sleep_seconds < self.min_cycle_sleep_seconds:
            raise ValueError("max_cycle_sleep_seconds must be >= min_cycle_sleep_seconds")


@dataclass
class ContinuousAnalysisState:
    """Durable checkpoint for restart-safe continuous research."""

    cycle: int = 0
    last_full_scan_at: str | None = None
    last_focus_scan_at: str | None = None
    last_result_at: str | None = None
    focus_symbols: list[str] = field(default_factory=list)
    chart_candidates: list[dict[str, Any]] = field(default_factory=list)
    latest_shortlist: list[dict[str, Any]] = field(default_factory=list)
    latest_decision: str = "NO_DATA"
    latest_phase: str = MarketPhase.CLOSED.value

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "ContinuousAnalysisState":
        raw = dict(payload or {})
        return cls(
            cycle=max(0, _int(raw.get("cycle"), 0)),
            last_full_scan_at=_optional_text(raw.get("last_full_scan_at")),
            last_focus_scan_at=_optional_text(raw.get("last_focus_scan_at")),
            last_result_at=_optional_text(raw.get("last_result_at")),
            focus_symbols=_symbols(raw.get("focus_symbols") or []),
            chart_candidates=[dict(x) for x in raw.get("chart_candidates", []) if isinstance(x, Mapping)],
            latest_shortlist=[dict(x) for x in raw.get("latest_shortlist", []) if isinstance(x, Mapping)],
            latest_decision=str(raw.get("latest_decision") or "NO_DATA"),
            latest_phase=str(raw.get("latest_phase") or MarketPhase.CLOSED.value),
        )


@dataclass(frozen=True)
class AnalysisPlan:
    """Pure decision for what one service cycle should do."""

    phase: MarketPhase
    full_scan: bool
    focus_refresh: bool
    checkpoint_outcomes: bool
    next_check_seconds: int
    reasons: tuple[str, ...]


class SessionProvider(Protocol):
    def __call__(self, now: datetime) -> TradingSession: ...


SymbolsProvider = Callable[[datetime], Sequence[str]]
OptionsProvider = Callable[[Sequence[str], datetime], Mapping[str, Sequence[Mapping[str, Any]]]]
CatalystProvider = Callable[[Sequence[str], datetime], Mapping[str, float]]


class AtomicAnalysisStateStore:
    """Small atomic JSON checkpoint store; safe across process restarts."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> ContinuousAnalysisState:
        if not self.path.exists():
            return ContinuousAnalysisState()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return ContinuousAnalysisState()
        return ContinuousAnalysisState.from_mapping(payload if isinstance(payload, Mapping) else {})

    def save(self, state: ContinuousAnalysisState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rendered = json.dumps(asdict(state), indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(rendered)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            try:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
            except OSError:
                pass


def classify_market_phase(
    now: datetime,
    session: TradingSession,
    config: ContinuousScanConfig | None = None,
) -> MarketPhase:
    """Classify ``now`` against an authoritative session schedule."""
    cfg = config or ContinuousScanConfig()
    cfg.validate()
    session.validate()
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if not session.trading_day:
        return MarketPhase.CLOSED

    current = now.astimezone(_UTC)
    open_at = session.open_at.astimezone(_UTC)
    close_at = session.close_at.astimezone(_UTC)
    premarket_start = open_at - timedelta(minutes=cfg.premarket_minutes)
    opening_end = open_at + timedelta(minutes=cfg.opening_minutes)
    power_hour_start = close_at - timedelta(minutes=cfg.power_hour_minutes)
    postmarket_end = close_at + timedelta(minutes=cfg.postmarket_minutes)

    if current < premarket_start or current >= postmarket_end:
        return MarketPhase.CLOSED
    if current < open_at:
        return MarketPhase.PREMARKET
    if current < min(opening_end, close_at):
        return MarketPhase.OPENING
    if current < max(open_at, power_hour_start):
        return MarketPhase.REGULAR
    if current < close_at:
        return MarketPhase.POWER_HOUR
    return MarketPhase.POSTMARKET


def plan_analysis_cycle(
    now: datetime,
    session: TradingSession,
    state: ContinuousAnalysisState,
    *,
    config: ContinuousScanConfig | None = None,
    force_full: bool = False,
    force_focus: bool = False,
) -> AnalysisPlan:
    """Choose full-universe vs focus-list work for one cycle.

    ``force_full`` is the on-demand path: it runs the same market-wide scanner
    immediately even outside regular hours.  It does not bypass data-quality,
    option, EV, risk, or execution gates elsewhere in the system.
    """
    cfg = config or ContinuousScanConfig()
    cfg.validate()
    phase = classify_market_phase(now, session, cfg)
    full_minutes = _full_scan_interval(phase, cfg)
    focus_minutes = _focus_interval(phase, cfg)
    last_full = _timestamp(state.last_full_scan_at)
    last_focus = _timestamp(state.last_focus_scan_at)

    full_due = force_full or _elapsed(now, last_full, full_minutes)
    focus_due = force_focus or force_full
    if focus_minutes is not None:
        focus_due = focus_due or _elapsed(now, last_focus, focus_minutes)
    elif not force_focus and not force_full:
        focus_due = False

    # A focus refresh without a prior chart universe has nothing reliable to
    # work on, so bootstrap with the cheap full scan first.
    if focus_due and not state.chart_candidates:
        full_due = True

    # Post-market cycles checkpoint outcomes for later calibration.  This is a
    # research/evaluation signal only; the outcomes module still enforces its
    # own anti-lookahead rules.
    checkpoint = phase is MarketPhase.POSTMARKET and (full_due or focus_due)

    reasons: list[str] = []
    if force_full:
        reasons.append("forced_full_analysis")
    elif full_due:
        reasons.append(f"{phase.value}_full_scan_due")
    if force_focus:
        reasons.append("forced_focus_refresh")
    elif focus_due:
        reasons.append(f"{phase.value}_focus_refresh_due")
    if checkpoint:
        reasons.append("postmarket_outcome_checkpoint")
    if not reasons:
        reasons.append("cadence_not_due")

    next_seconds = _next_check_seconds(now, last_full, last_focus, phase, cfg)
    return AnalysisPlan(
        phase=phase,
        full_scan=full_due,
        focus_refresh=focus_due,
        checkpoint_outcomes=checkpoint,
        next_check_seconds=next_seconds,
        reasons=tuple(reasons),
    )


class ContinuousOptionsAnalyzer:
    """Run the existing scanner repeatedly against point-in-time data.

    The full stage reads every supplied U.S. symbol from ``OptionsResearchStore``
    and computes the cross-sectional chart screen.  Only the strongest focus
    list is then handed to the injected options/catalyst providers, keeping the
    expensive work bounded while the entire market remains the starting set.
    """

    def __init__(
        self,
        *,
        store: OptionsResearchStore,
        symbols_provider: SymbolsProvider,
        state_store: AtomicAnalysisStateStore,
        options_provider: OptionsProvider | None = None,
        catalyst_provider: CatalystProvider | None = None,
        config: ContinuousScanConfig | None = None,
    ) -> None:
        self.store = store
        self.symbols_provider = symbols_provider
        self.state_store = state_store
        self.options_provider = options_provider
        self.catalyst_provider = catalyst_provider
        self.config = config or ContinuousScanConfig()
        self.config.validate()

    def run_cycle(
        self,
        *,
        now: datetime,
        session: TradingSession,
        force_full: bool = False,
        force_focus: bool = False,
    ) -> dict[str, Any]:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        state = self.state_store.load()
        plan = plan_analysis_cycle(
            now,
            session,
            state,
            config=self.config,
            force_full=force_full,
            force_focus=force_focus,
        )
        actions: list[dict[str, Any]] = []
        chart_stage: dict[str, Any] | None = None
        final_stage: dict[str, Any] | None = None

        if plan.full_scan:
            symbols = _symbols(self.symbols_provider(now))
            start = now - timedelta(days=self.config.history_days)
            frames = self.store.equity_frames_asof(
                symbols,
                start=start,
                end=now,
                as_of=now,
                interval="1D",
            )
            chart_stage = scan_market_frames(
                frames,
                ChartScreenConfig(top_n=self.config.max_focus_symbols),
            )
            candidates = [dict(row) for row in chart_stage.get("candidates", []) if isinstance(row, Mapping)]
            state.chart_candidates = candidates[: self.config.max_focus_symbols]
            state.focus_symbols = _symbols(row.get("symbol") for row in state.chart_candidates)
            state.last_full_scan_at = _iso(now)
            actions.append(
                {
                    "action": "full_universe_chart_scan",
                    "symbols_requested": len(symbols),
                    "symbols_with_point_in_time_frames": len(frames),
                    "focus_count": len(state.focus_symbols),
                }
            )

        if plan.focus_refresh:
            deep_candidates = state.chart_candidates[: self.config.max_deep_symbols]
            deep_symbols = _symbols(row.get("symbol") for row in deep_candidates)
            if self.options_provider is None:
                final_stage = {
                    "decision": "NO_TRADE",
                    "candidate_count": 0,
                    "candidates": [],
                    "reason": "options_provider_not_configured",
                }
                actions.append({"action": "focus_refresh_skipped", "reason": "options_provider_not_configured"})
            else:
                options = self.options_provider(deep_symbols, now)
                catalysts = self.catalyst_provider(deep_symbols, now) if self.catalyst_provider else {}
                final_stage = combine_rankings(
                    deep_candidates,
                    options,
                    catalyst_scores=catalysts,
                    config=OptionsMarketPipelineConfig(
                        target_profit_pct=self.config.target_profit_pct,
                        final_top_n=self.config.final_top_n,
                    ),
                )
                actions.append(
                    {
                        "action": "focus_options_rerank",
                        "symbols": len(deep_symbols),
                        "catalyst_scores": len(catalysts),
                    }
                )
            state.last_focus_scan_at = _iso(now)

        if final_stage is not None:
            state.latest_decision = str(final_stage.get("decision") or "NO_TRADE")
            state.latest_shortlist = [
                dict(row) for row in final_stage.get("candidates", []) if isinstance(row, Mapping)
            ][: self.config.final_top_n]
            state.last_result_at = _iso(now)
        elif chart_stage is not None:
            state.latest_decision = "CHART_FOCUS_READY" if state.focus_symbols else "NO_TRADE"
            state.last_result_at = _iso(now)

        state.cycle += 1
        state.latest_phase = plan.phase.value
        self.state_store.save(state)

        return {
            "mode": "continuous_us_options_analysis",
            "as_of": _iso(now),
            "phase": plan.phase.value,
            "calendar_source": session.source,
            "calendar_authoritative": session.authoritative,
            "cycle": state.cycle,
            "plan": {
                "full_scan": plan.full_scan,
                "focus_refresh": plan.focus_refresh,
                "checkpoint_outcomes": plan.checkpoint_outcomes,
                "next_check_seconds": plan.next_check_seconds,
                "reasons": list(plan.reasons),
            },
            "actions": actions,
            "chart_stage": chart_stage,
            "final_stage": final_stage,
            "latest_decision": state.latest_decision,
            "latest_shortlist": state.latest_shortlist,
            "focus_symbols": state.focus_symbols,
            "warning": (
                "Research loop only. Continuous analysis does not imply continuous trading. "
                "Broker execution remains a separate, paper-only, fail-closed gate."
            ),
        }


def weekday_regular_session(now: datetime) -> TradingSession:
    """Development fallback for a normal weekday 09:30-16:00 ET session.

    This helper is intentionally marked non-authoritative because it does not
    encode exchange holidays or early closes.  Production services should inject
    a real exchange/session provider instead of treating weekday arithmetic as a
    trading calendar.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(_ET)
    day = local.date()
    open_at = datetime(day.year, day.month, day.day, 9, 30, tzinfo=_ET)
    close_at = datetime(day.year, day.month, day.day, 16, 0, tzinfo=_ET)
    return TradingSession(
        open_at=open_at,
        close_at=close_at,
        trading_day=local.weekday() < 5,
        source="weekday_fallback",
        authoritative=False,
    )


def _full_scan_interval(phase: MarketPhase, cfg: ContinuousScanConfig) -> int:
    return {
        MarketPhase.CLOSED: cfg.closed_full_scan_minutes,
        MarketPhase.PREMARKET: cfg.premarket_full_scan_minutes,
        MarketPhase.OPENING: cfg.opening_full_scan_minutes,
        MarketPhase.REGULAR: cfg.regular_full_scan_minutes,
        MarketPhase.POWER_HOUR: cfg.power_hour_full_scan_minutes,
        MarketPhase.POSTMARKET: cfg.postmarket_full_scan_minutes,
    }[phase]


def _focus_interval(phase: MarketPhase, cfg: ContinuousScanConfig) -> int | None:
    return {
        MarketPhase.CLOSED: None,
        MarketPhase.PREMARKET: cfg.premarket_focus_minutes,
        MarketPhase.OPENING: cfg.opening_focus_minutes,
        MarketPhase.REGULAR: cfg.regular_focus_minutes,
        MarketPhase.POWER_HOUR: cfg.power_hour_focus_minutes,
        MarketPhase.POSTMARKET: cfg.postmarket_focus_minutes,
    }[phase]


def _next_check_seconds(
    now: datetime,
    last_full: datetime | None,
    last_focus: datetime | None,
    phase: MarketPhase,
    cfg: ContinuousScanConfig,
) -> int:
    waits: list[float] = []
    full_due = _due_in_seconds(now, last_full, _full_scan_interval(phase, cfg))
    waits.append(full_due)
    focus_minutes = _focus_interval(phase, cfg)
    if focus_minutes is not None:
        waits.append(_due_in_seconds(now, last_focus, focus_minutes))
    seconds = int(max(0.0, min(waits) if waits else cfg.max_cycle_sleep_seconds))
    return max(cfg.min_cycle_sleep_seconds, min(cfg.max_cycle_sleep_seconds, seconds or cfg.min_cycle_sleep_seconds))


def _elapsed(now: datetime, last: datetime | None, minutes: int) -> bool:
    if last is None:
        return True
    return now.astimezone(_UTC) >= last.astimezone(_UTC) + timedelta(minutes=minutes)


def _due_in_seconds(now: datetime, last: datetime | None, minutes: int) -> float:
    if last is None:
        return 0.0
    due = last.astimezone(_UTC) + timedelta(minutes=minutes)
    return max(0.0, (due - now.astimezone(_UTC)).total_seconds())


def _timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=_UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(_UTC).isoformat().replace("+00:00", "Z")


def _symbols(values: Sequence[Any] | Any) -> list[str]:
    if isinstance(values, (str, bytes)):
        iterable = [values]
    else:
        try:
            iterable = list(values)
        except TypeError:
            iterable = [values]
    result: list[str] = []
    seen: set[str] = set()
    for value in iterable:
        symbol = str(value or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        result.append(symbol)
    return result


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


__all__ = [
    "AnalysisPlan",
    "AtomicAnalysisStateStore",
    "ContinuousAnalysisState",
    "ContinuousOptionsAnalyzer",
    "ContinuousScanConfig",
    "MarketPhase",
    "TradingSession",
    "classify_market_phase",
    "plan_analysis_cycle",
    "weekday_regular_session",
]
