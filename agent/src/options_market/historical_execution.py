"""Retrospective long-option execution realism for historical research.

The model never assumes every displayed ask was fillable. A candidate is treated
as a buy-limit order submitted at the historical decision time. After configured
latency, later quote observations are inspected until the wait window expires.
The order fills only when an eligible ask is at or below the submitted limit.

Without order-book queue/size history, fill certainty cannot be reconstructed.
For conservatism a triggered buy fills at the submitted limit price, not a
midpoint or better displayed ask. Results are evaluation labels only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import math
from statistics import median
from typing import Any, Iterable, Mapping

import pandas as pd

from .outcomes import OutcomeConfig, label_long_option_path

UTC = timezone.utc


@dataclass(frozen=True)
class HistoricalExecutionConfig:
    latency_seconds: float = 1.0
    max_wait_seconds: float = 120.0
    max_chase_pct: float = 0.0
    max_spread_pct: float = 20.0
    fill_at_limit: bool = True

    def validate(self) -> None:
        if self.latency_seconds < 0:
            raise ValueError("latency_seconds cannot be negative")
        if self.max_wait_seconds <= 0:
            raise ValueError("max_wait_seconds must be positive")
        if not 0 <= self.max_chase_pct <= 25:
            raise ValueError("max_chase_pct must be between 0 and 25")
        if not 0 < self.max_spread_pct <= 100:
            raise ValueError("max_spread_pct must be > 0 and <= 100")


def simulate_long_option_entry(
    candidate: Mapping[str, Any],
    quotes: pd.DataFrame,
    *,
    config: HistoricalExecutionConfig | None = None,
) -> dict[str, Any]:
    """Simulate one historical long-option buy-limit entry."""
    cfg = config or HistoricalExecutionConfig()
    cfg.validate()
    decision_time = _candidate_time(candidate)
    contract = str(candidate.get("contract_symbol") or "").strip().upper().replace(" ", "")
    decision_ask = _positive(candidate.get("entry_ask"))
    decision_bid = _nonnegative(candidate.get("bid"))
    if decision_time is None:
        return _blocked("decision_time_required", contract=contract, config=cfg)
    if not contract:
        return _blocked("contract_symbol_required", contract=None, config=cfg)
    if decision_ask is None:
        return _blocked("decision_ask_required", contract=contract, config=cfg)

    decision_spread = _spread_pct(decision_bid, decision_ask)
    if decision_spread is not None and decision_spread > cfg.max_spread_pct:
        return {
            **_base(contract, decision_time, decision_ask, decision_bid, cfg),
            "status": "UNFILLED",
            "filled": False,
            "reason": "decision_spread_too_wide",
            "decision_spread_pct": round(decision_spread, 4),
            "warnings": ["historical_fill_rejected_by_spread_gate"],
        }

    explicit_limit = _positive(candidate.get("execution_limit_price") or candidate.get("limit_price"))
    limit_price = explicit_limit or decision_ask * (1.0 + cfg.max_chase_pct / 100.0)
    eligible_at = decision_time + timedelta(seconds=cfg.latency_seconds)
    expires_at = decision_time + timedelta(seconds=cfg.max_wait_seconds)
    frame = _normalize_quote_path(quotes)
    resolution = _quote_resolution_seconds(frame)
    warnings: list[str] = []
    if resolution is not None and resolution > cfg.max_wait_seconds:
        warnings.append("quote_resolution_coarse_for_wait_window")
    if "ask_size" not in frame.columns:
        warnings.append("historical_quote_size_unavailable_queue_fill_unknown")

    if frame.empty:
        return {
            **_base(contract, decision_time, decision_ask, decision_bid, cfg),
            "status": "DATA_UNAVAILABLE",
            "filled": False,
            "reason": "no_usable_quote_path",
            "limit_price": round(limit_price, 6),
            "eligible_at": eligible_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "warnings": warnings,
        }

    eligible = frame[
        (frame["event_ts"] >= eligible_at)
        & (frame["event_ts"] <= expires_at)
    ].copy()
    if eligible.empty:
        return {
            **_base(contract, decision_time, decision_ask, decision_bid, cfg),
            "status": "DATA_UNAVAILABLE",
            "filled": False,
            "reason": "no_quote_after_latency_within_wait_window",
            "limit_price": round(limit_price, 6),
            "eligible_at": eligible_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "quote_resolution_seconds": resolution,
            "warnings": warnings,
        }

    trigger_row: pd.Series | None = None
    rejected_wide = 0
    for _, row in eligible.iterrows():
        ask = _positive(row.get("ask"))
        bid = _nonnegative(row.get("bid"))
        if ask is None or bid is None or ask < bid:
            continue
        spread = _spread_pct(bid, ask)
        if spread is not None and spread > cfg.max_spread_pct:
            rejected_wide += 1
            continue
        if ask <= limit_price:
            trigger_row = row
            break

    if trigger_row is None:
        if rejected_wide:
            warnings.append("eligible_quotes_rejected_by_spread_gate")
        return {
            **_base(contract, decision_time, decision_ask, decision_bid, cfg),
            "status": "UNFILLED",
            "filled": False,
            "reason": "limit_not_reached_before_timeout",
            "limit_price": round(limit_price, 6),
            "eligible_at": eligible_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "eligible_quote_count": int(len(eligible)),
            "quote_resolution_seconds": resolution,
            "warnings": warnings,
        }

    trigger_ask = _positive(trigger_row.get("ask"))
    trigger_bid = _nonnegative(trigger_row.get("bid"))
    assert trigger_ask is not None
    fill_price = limit_price if cfg.fill_at_limit else min(limit_price, trigger_ask)
    fill_time = _aware_time(trigger_row.get("event_ts"))
    assert fill_time is not None
    wait_seconds = max(0.0, (fill_time - decision_time).total_seconds())
    shortfall_pct = (fill_price / decision_ask - 1.0) * 100.0
    shortfall_usd = (fill_price - decision_ask) * 100.0
    return {
        **_base(contract, decision_time, decision_ask, decision_bid, cfg),
        "status": "FILLED",
        "filled": True,
        "reason": "limit_triggered",
        "limit_price": round(limit_price, 6),
        "eligible_at": eligible_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "fill_time": fill_time.isoformat(),
        "fill_price": round(fill_price, 6),
        "trigger_bid": None if trigger_bid is None else round(trigger_bid, 6),
        "trigger_ask": round(trigger_ask, 6),
        "wait_seconds": round(wait_seconds, 3),
        "implementation_shortfall_vs_decision_ask_pct": round(shortfall_pct, 4),
        "implementation_shortfall_usd_per_contract": round(shortfall_usd, 2),
        "quote_resolution_seconds": resolution,
        "warnings": warnings,
    }


def label_filled_execution_outcome(
    candidate: Mapping[str, Any],
    execution: Mapping[str, Any],
    outcome_quotes: pd.DataFrame,
    *,
    outcome_config: OutcomeConfig | None = None,
) -> dict[str, Any] | None:
    """Label a filled candidate from simulated fill price using later bids."""
    if not bool(execution.get("filled")):
        return None
    fill_price = _positive(execution.get("fill_price"))
    fill_time = _aware_time(execution.get("fill_time"))
    if fill_price is None or fill_time is None:
        return None
    frame = _normalize_quote_path(outcome_quotes)
    frame = frame[frame["event_ts"] >= fill_time].copy()
    if frame.empty:
        return None
    indexed = frame.set_index(pd.DatetimeIndex(frame["event_ts"]))
    adjusted = dict(candidate)
    adjusted["entry_ask"] = fill_price
    outcome = label_long_option_path(
        adjusted,
        indexed[["bid"]],
        config=outcome_config or OutcomeConfig(
            target_profit_pct=_positive(candidate.get("target_profit_pct")) or 300.0
        ),
    )
    outcome["decision_entry_ask"] = _positive(candidate.get("entry_ask"))
    outcome["simulated_fill_price"] = fill_price
    outcome["simulated_fill_time"] = fill_time.isoformat()
    outcome["execution_status"] = "FILLED"
    return outcome


def summarize_historical_execution(results: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows = [dict(row) for row in results]
    total = len(rows)
    filled = [row for row in rows if row.get("status") == "FILLED"]
    unfilled = [row for row in rows if row.get("status") == "UNFILLED"]
    unavailable = [row for row in rows if row.get("status") == "DATA_UNAVAILABLE"]
    shortfalls = [
        value
        for row in filled
        if (value := _finite(row.get("implementation_shortfall_vs_decision_ask_pct"))) is not None
    ]
    waits = [value for row in filled if (value := _finite(row.get("wait_seconds"))) is not None]
    return {
        "candidates": total,
        "filled": len(filled),
        "unfilled": len(unfilled),
        "data_unavailable": len(unavailable),
        "fill_rate": 0.0 if total == 0 else round(len(filled) / total, 4),
        "fill_rate_when_observable": (
            0.0
            if len(filled) + len(unfilled) == 0
            else round(len(filled) / (len(filled) + len(unfilled)), 4)
        ),
        "mean_implementation_shortfall_pct": (
            None if not shortfalls else round(sum(shortfalls) / len(shortfalls), 4)
        ),
        "median_wait_seconds": None if not waits else round(median(waits), 3),
        "warning": (
            "Historical quote-trigger model only. Without queue position, quote size and full order-book history, a triggered limit is not proof that a real order would have filled."
        ),
    }


def _normalize_quote_path(quotes: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(quotes, pd.DataFrame) or quotes.empty:
        return pd.DataFrame(columns=["event_ts", "bid", "ask"])
    frame = quotes.copy()
    if "event_ts" not in frame.columns:
        if isinstance(frame.index, pd.DatetimeIndex):
            frame["event_ts"] = frame.index
        else:
            return pd.DataFrame(columns=["event_ts", "bid", "ask"])
    for column in ("bid", "ask"):
        if column not in frame.columns:
            return pd.DataFrame(columns=["event_ts", "bid", "ask"])
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["event_ts"] = pd.to_datetime(frame["event_ts"], utc=True, errors="coerce")
    frame = frame.dropna(subset=["event_ts", "bid", "ask"])
    frame = frame[(frame["bid"] >= 0.0) & (frame["ask"] > 0.0) & (frame["ask"] >= frame["bid"])]
    return frame.sort_values("event_ts").drop_duplicates(subset=["event_ts"], keep="last").reset_index(drop=True)


def _quote_resolution_seconds(frame: pd.DataFrame) -> float | None:
    if len(frame) < 2:
        return None
    diffs = frame["event_ts"].sort_values().diff().dt.total_seconds().dropna()
    diffs = diffs[diffs > 0]
    if diffs.empty:
        return None
    return round(float(diffs.median()), 3)


def _candidate_time(candidate: Mapping[str, Any]) -> datetime | None:
    for field in ("research_time", "entry_time", "decision_time"):
        value = _aware_time(candidate.get(field))
        if value is not None:
            return value
    return None


def _base(
    contract: str,
    decision_time: datetime,
    decision_ask: float,
    decision_bid: float | None,
    config: HistoricalExecutionConfig,
) -> dict[str, Any]:
    return {
        "contract_symbol": contract,
        "decision_time": decision_time.isoformat(),
        "decision_bid": None if decision_bid is None else round(decision_bid, 6),
        "decision_ask": round(decision_ask, 6),
        "config": asdict(config),
        "evaluation_only": True,
        "broker_mutation": False,
    }


def _blocked(reason: str, *, contract: str | None, config: HistoricalExecutionConfig) -> dict[str, Any]:
    return {
        "contract_symbol": contract,
        "status": "DATA_UNAVAILABLE",
        "filled": False,
        "reason": reason,
        "config": asdict(config),
        "warnings": [],
        "evaluation_only": True,
        "broker_mutation": False,
    }


def _spread_pct(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None or ask < bid:
        return None
    midpoint = (bid + ask) / 2.0
    if midpoint <= 0:
        return None
    return (ask - bid) / midpoint * 100.0


def _aware_time(value: object) -> datetime | None:
    if value is None:
        return None
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        return None
    return pd.Timestamp(parsed).to_pydatetime().astimezone(UTC)


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


def _nonnegative(value: object) -> float | None:
    number = _finite(value)
    return number if number is not None and number >= 0 else None


__all__ = [
    "HistoricalExecutionConfig",
    "label_filled_execution_outcome",
    "simulate_long_option_entry",
    "summarize_historical_execution",
]
