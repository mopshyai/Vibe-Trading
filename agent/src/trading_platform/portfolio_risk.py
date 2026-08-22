"""Account-level risk aggregation for the personal options platform.

This module complements the per-candidate premium-risk gate. It answers a
different question: "given everything already open, should the account be allowed
to take additional risk?"

The implementation is broker-neutral and research/paper safe. It only consumes
supplied account/position/return data and never imports broker mutation code.
Long-premium options are the supported default; unsupported short option exposure
fails closed because max loss cannot be inferred from premium paid.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import math
import re
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

from src.quantlib.risk import max_drawdown_analysis

from .models import RiskSummary

_OCC_RE = re.compile(r"^([A-Z0-9]{1,6})(\d{6})([CP])(\d{8})$")


@dataclass(frozen=True)
class AccountRiskConfig:
    """Software defaults for conservative research/paper portfolio controls."""

    max_total_premium_risk_pct: float = 5.0
    max_underlying_risk_pct: float = 1.5
    max_sector_risk_pct: float = 2.5
    max_expiry_risk_pct: float = 2.5
    max_correlated_cluster_risk_pct: float = 3.0
    correlation_threshold: float = 0.75
    max_daily_loss_pct: float = 2.0
    max_weekly_loss_pct: float = 4.0
    max_drawdown_pct: float = 8.0
    max_positions: int = 6
    require_complete_premium_risk: bool = True
    block_unsupported_short_options: bool = True

    def validate(self) -> None:
        for name, value in (
            ("max_total_premium_risk_pct", self.max_total_premium_risk_pct),
            ("max_underlying_risk_pct", self.max_underlying_risk_pct),
            ("max_sector_risk_pct", self.max_sector_risk_pct),
            ("max_expiry_risk_pct", self.max_expiry_risk_pct),
            ("max_correlated_cluster_risk_pct", self.max_correlated_cluster_risk_pct),
            ("max_daily_loss_pct", self.max_daily_loss_pct),
            ("max_weekly_loss_pct", self.max_weekly_loss_pct),
            ("max_drawdown_pct", self.max_drawdown_pct),
        ):
            if not 0 < value <= 100:
                raise ValueError(f"{name} must be > 0 and <= 100")
        if not 0 < self.correlation_threshold <= 1:
            raise ValueError("correlation_threshold must be > 0 and <= 1")
        if self.max_positions < 1:
            raise ValueError("max_positions must be at least 1")


def assess_account_portfolio_risk(
    positions: Iterable[Mapping[str, Any]],
    *,
    account_equity_usd: float,
    daily_realized_pnl_usd: float | None = None,
    weekly_realized_pnl_usd: float | None = None,
    equity_curve: pd.Series | Iterable[float] | None = None,
    returns_by_underlying: Mapping[str, pd.Series | Iterable[float]] | None = None,
    config: AccountRiskConfig | None = None,
) -> dict[str, Any]:
    """Aggregate account risk and fail closed on configured breaches.

    Position input is intentionally tolerant of common broker/research field
    names. For option premium risk, total ``premium_risk_usd`` or broker
    ``cost_basis`` is preferred. If neither is available, ``avg_entry_price`` is
    multiplied by quantity and the contract multiplier.
    """
    cfg = config or AccountRiskConfig()
    cfg.validate()
    equity = _positive(account_equity_usd)
    if equity is None:
        return _invalid_report(cfg, "account_equity_usd must be a positive finite number")

    normalized: list[dict[str, Any]] = []
    normalization_errors: list[str] = []
    for index, raw in enumerate(positions):
        row, errors = _normalize_position(raw, index=index)
        normalization_errors.extend(errors)
        if row is not None:
            normalized.append(row)

    reasons: list[str] = []
    warnings: list[str] = []
    if cfg.require_complete_premium_risk:
        missing = [
            row["contract_symbol"] or row["underlying"]
            for row in normalized
            if row["premium_risk_usd"] is None
        ]
        if missing:
            reasons.append("incomplete_position_premium_risk")
            warnings.append(f"Missing premium-risk basis for {len(missing)} position(s).")
    if normalization_errors:
        warnings.extend(normalization_errors)
        if cfg.block_unsupported_short_options and any(
            "short_option" in item for item in normalization_errors
        ):
            reasons.append("unsupported_short_option_exposure")

    risk_rows = [row for row in normalized if row["premium_risk_usd"] is not None]
    total_risk = sum(float(row["premium_risk_usd"]) for row in risk_rows)
    total_risk_pct = total_risk / equity * 100.0
    if total_risk_pct > cfg.max_total_premium_risk_pct:
        reasons.append("total_open_premium_risk_limit")
    if len(normalized) > cfg.max_positions:
        reasons.append("max_positions")

    by_underlying = _risk_buckets(risk_rows, "underlying", equity)
    by_sector = _risk_buckets(risk_rows, "sector", equity, ignore_empty=True)
    by_expiry = _risk_buckets(risk_rows, "expiration", equity, ignore_empty=True)

    if any(row["risk_pct"] > cfg.max_underlying_risk_pct for row in by_underlying):
        reasons.append("underlying_concentration_limit")
    if any(row["risk_pct"] > cfg.max_sector_risk_pct for row in by_sector):
        reasons.append("sector_concentration_limit")
    if any(row["risk_pct"] > cfg.max_expiry_risk_pct for row in by_expiry):
        reasons.append("expiry_concentration_limit")

    greeks = _aggregate_greeks(normalized, equity)
    daily_loss_pct = _loss_pct(daily_realized_pnl_usd, equity)
    weekly_loss_pct = _loss_pct(weekly_realized_pnl_usd, equity)
    if daily_loss_pct is not None and daily_loss_pct > cfg.max_daily_loss_pct:
        reasons.append("daily_loss_kill_threshold")
    if weekly_loss_pct is not None and weekly_loss_pct > cfg.max_weekly_loss_pct:
        reasons.append("weekly_loss_kill_threshold")

    drawdown = _drawdown(equity_curve)
    if drawdown is not None and drawdown["max_drawdown_pct"] > cfg.max_drawdown_pct:
        reasons.append("account_drawdown_kill_threshold")

    correlations = _correlation_clusters(
        risk_rows,
        returns_by_underlying or {},
        equity=equity,
        threshold=cfg.correlation_threshold,
    )
    if any(
        cluster["risk_pct"] > cfg.max_correlated_cluster_risk_pct
        for cluster in correlations["clusters"]
    ):
        reasons.append("correlated_cluster_concentration_limit")

    blocked = bool(reasons)
    return {
        "approved": not blocked,
        "trading_blocked": blocked,
        "decision": "ACCOUNT_RISK_APPROVED" if not blocked else "ACCOUNT_RISK_BLOCKED",
        "blocking_reasons": _dedupe(reasons),
        "warnings": _dedupe(warnings),
        "account_equity_usd": round(equity, 2),
        "position_count": len(normalized),
        "positions_with_known_premium_risk": len(risk_rows),
        "open_premium_risk_usd": round(total_risk, 2),
        "open_premium_risk_pct": round(total_risk_pct, 4),
        "daily_realized_pnl_usd": _round_or_none(daily_realized_pnl_usd),
        "daily_loss_pct": _round_or_none(daily_loss_pct),
        "weekly_realized_pnl_usd": _round_or_none(weekly_realized_pnl_usd),
        "weekly_loss_pct": _round_or_none(weekly_loss_pct),
        "drawdown": drawdown,
        "greeks": greeks,
        "concentration": {
            "underlying": by_underlying,
            "sector": by_sector,
            "expiry": by_expiry,
            "correlation": correlations,
        },
        "normalized_positions": normalized,
        "config": asdict(cfg),
        "warning": (
            "Account-level research/paper risk controls only. Risk estimates depend on supplied positions, prices, "
            "Greeks and history and cannot guarantee against loss."
        ),
    }


def risk_summary_from_report(report: Mapping[str, Any]) -> RiskSummary:
    """Project the detailed portfolio report into the Trading Desk summary."""
    drawdown = report.get("drawdown") if isinstance(report.get("drawdown"), Mapping) else {}
    return RiskSummary(
        account_equity_usd=_positive(report.get("account_equity_usd")),
        open_premium_risk_usd=_nonnegative(report.get("open_premium_risk_usd")),
        open_premium_risk_pct=_nonnegative(report.get("open_premium_risk_pct")),
        daily_realized_pnl_usd=_finite(report.get("daily_realized_pnl_usd")),
        weekly_realized_pnl_usd=_finite(report.get("weekly_realized_pnl_usd")),
        max_drawdown_pct=_nonnegative(drawdown.get("max_drawdown_pct")),
        positions=_nonnegative_int(report.get("position_count")) or 0,
        trading_blocked=bool(report.get("trading_blocked")),
        blocking_reasons=[str(item) for item in report.get("blocking_reasons", [])],
        greeks=dict(report.get("greeks") or {}),
        concentration=dict(report.get("concentration") or {}),
    )


def _normalize_position(
    raw: Mapping[str, Any],
    *,
    index: int,
) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    symbol = str(raw.get("symbol") or raw.get("contract_symbol") or "").strip().upper()
    contract_symbol = str(raw.get("contract_symbol") or symbol or "").strip().upper() or None
    occ = _parse_occ(contract_symbol)
    underlying = str(
        raw.get("underlying") or (occ or {}).get("underlying") or symbol
    ).strip().upper()
    if underlying.endswith(".US"):
        underlying = underlying[:-3]
    if not underlying:
        return None, [f"position_{index}:missing_underlying"]

    quantity_raw = _finite(
        raw.get("quantity") if raw.get("quantity") is not None else raw.get("qty")
    )
    quantity = 1.0 if quantity_raw is None else quantity_raw
    side = str(raw.get("side") or "long").strip().lower()
    is_short = quantity < 0 or side in {"short", "sell", "short_sell"}
    quantity_abs = abs(quantity)
    asset_class = str(
        raw.get("asset_class") or ("option" if occ or raw.get("option_type") else "unknown")
    ).lower()
    if asset_class == "option" and is_short:
        errors.append(f"position_{index}:short_option_requires_defined_max_loss")

    option_type = str(
        raw.get("option_type") or (occ or {}).get("option_type") or ""
    ).lower() or None
    expiration = _expiration_text(raw.get("expiration") or (occ or {}).get("expiration"))
    multiplier = _positive(raw.get("multiplier")) or (100.0 if asset_class == "option" else 1.0)

    premium_risk = _nonnegative(raw.get("premium_risk_usd"))
    if premium_risk is None:
        # Alpaca's position cost_basis is already the total basis for the position.
        premium_risk = _abs_finite(raw.get("cost_basis"))
    if premium_risk is None and not is_short:
        per_contract_loss = _positive(raw.get("max_loss_usd_per_contract"))
        if per_contract_loss is not None:
            premium_risk = per_contract_loss * quantity_abs
    if premium_risk is None and not is_short:
        avg_entry = _positive(
            raw.get("avg_entry_price")
            or raw.get("average_cost")
            or raw.get("entry_price")
        )
        if avg_entry is not None:
            premium_risk = avg_entry * multiplier * quantity_abs

    delta = _finite(raw.get("delta"))
    gamma = _finite(raw.get("gamma"))
    theta = _finite(raw.get("theta"))
    vega = _finite(raw.get("vega"))
    underlying_price = _positive(raw.get("underlying_price") or raw.get("spot"))

    sign = -1.0 if is_short else 1.0
    delta_shares = None if delta is None else delta * multiplier * quantity_abs * sign
    dollar_delta = (
        None
        if delta_shares is None or underlying_price is None
        else delta_shares * underlying_price
    )
    gamma_scaled = None if gamma is None else gamma * multiplier * quantity_abs * sign
    theta_scaled = None if theta is None else theta * multiplier * quantity_abs * sign
    vega_scaled = None if vega is None else vega * multiplier * quantity_abs * sign

    return {
        "symbol": symbol or underlying,
        "contract_symbol": contract_symbol,
        "underlying": underlying,
        "asset_class": asset_class,
        "option_type": option_type,
        "expiration": expiration,
        "sector": str(raw.get("sector") or "").strip().lower(),
        "theme": str(raw.get("theme") or "").strip().lower(),
        "quantity": round(quantity_abs, 8),
        "side": "short" if is_short else "long",
        "multiplier": multiplier,
        "premium_risk_usd": _round_or_none(premium_risk),
        "market_value_usd": _round_or_none(
            _abs_finite(raw.get("market_value") or raw.get("market_value_usd"))
        ),
        "underlying_price": _round_or_none(underlying_price),
        "delta": delta,
        "gamma": gamma,
        "theta": theta,
        "vega": vega,
        "delta_shares": _round_or_none(delta_shares),
        "dollar_delta_usd": _round_or_none(dollar_delta),
        "gamma_scaled": _round_or_none(gamma_scaled),
        "theta_scaled_per_day": _round_or_none(theta_scaled),
        "vega_scaled": _round_or_none(vega_scaled),
    }, errors


def _aggregate_greeks(rows: list[Mapping[str, Any]], equity: float) -> dict[str, Any]:
    fields = {
        "delta_shares": "delta_shares",
        "dollar_delta_usd": "dollar_delta_usd",
        "gamma_scaled": "gamma_scaled",
        "theta_scaled_per_day": "theta_scaled_per_day",
        "vega_scaled": "vega_scaled",
    }
    output: dict[str, Any] = {}
    coverage: dict[str, dict[str, int]] = {}
    for output_name, field in fields.items():
        values = [_finite(row.get(field)) for row in rows]
        known = [value for value in values if value is not None]
        output[output_name] = round(sum(known), 6) if known else None
        coverage[output_name] = {"known": len(known), "positions": len(rows)}
    dollar_delta = _finite(output.get("dollar_delta_usd"))
    output["dollar_delta_pct_equity"] = (
        None if dollar_delta is None else round(dollar_delta / equity * 100.0, 4)
    )
    output["coverage"] = coverage
    return output


def _risk_buckets(
    rows: list[Mapping[str, Any]],
    field: str,
    equity: float,
    *,
    ignore_empty: bool = False,
) -> list[dict[str, Any]]:
    totals: dict[str, float] = {}
    for row in rows:
        key = str(row.get(field) or "").strip()
        if not key and ignore_empty:
            continue
        key = key or "unknown"
        risk = _nonnegative(row.get("premium_risk_usd")) or 0.0
        totals[key] = totals.get(key, 0.0) + risk
    result = [
        {
            "key": key,
            "risk_usd": round(value, 2),
            "risk_pct": round(value / equity * 100.0, 4),
        }
        for key, value in totals.items()
    ]
    result.sort(key=lambda row: (-float(row["risk_usd"]), str(row["key"])))
    return result


def _correlation_clusters(
    risk_rows: list[Mapping[str, Any]],
    returns_by_underlying: Mapping[str, pd.Series | Iterable[float]],
    *,
    equity: float,
    threshold: float,
) -> dict[str, Any]:
    risk_by_symbol: dict[str, float] = {}
    for row in risk_rows:
        symbol = str(row.get("underlying") or "").upper()
        if symbol:
            risk_by_symbol[symbol] = risk_by_symbol.get(symbol, 0.0) + (
                _nonnegative(row.get("premium_risk_usd")) or 0.0
            )

    series: dict[str, pd.Series] = {}
    for symbol in risk_by_symbol:
        raw = returns_by_underlying.get(symbol)
        if raw is None:
            raw = returns_by_underlying.get(f"{symbol}.US")
        if raw is None:
            continue
        clean = pd.Series(raw, dtype=float).replace([np.inf, -np.inf], np.nan).dropna()
        if len(clean) >= 20:
            series[symbol] = clean

    if len(series) < 2:
        return {
            "threshold": threshold,
            "underlyings_with_history": len(series),
            "pairs": [],
            "clusters": [],
        }

    frame = pd.concat(series, axis=1, join="inner").dropna()
    if len(frame) < 20:
        return {
            "threshold": threshold,
            "underlyings_with_history": len(series),
            "pairs": [],
            "clusters": [],
        }
    corr = frame.corr()
    graph: dict[str, set[str]] = {symbol: set() for symbol in corr.columns}
    pairs: list[dict[str, Any]] = []
    columns = list(corr.columns)
    for i, left in enumerate(columns):
        for right in columns[i + 1:]:
            value = float(corr.loc[left, right])
            if not math.isfinite(value):
                continue
            if abs(value) >= threshold:
                graph[left].add(right)
                graph[right].add(left)
                pairs.append(
                    {"left": left, "right": right, "correlation": round(value, 4)}
                )

    visited: set[str] = set()
    clusters: list[dict[str, Any]] = []
    for symbol in columns:
        if symbol in visited or not graph[symbol]:
            continue
        stack = [symbol]
        component: set[str] = set()
        while stack:
            current = stack.pop()
            if current in component:
                continue
            component.add(current)
            stack.extend(graph[current] - component)
        visited.update(component)
        risk = sum(risk_by_symbol.get(item, 0.0) for item in component)
        clusters.append(
            {
                "underlyings": sorted(component),
                "risk_usd": round(risk, 2),
                "risk_pct": round(risk / equity * 100.0, 4),
            }
        )
    clusters.sort(key=lambda row: -float(row["risk_usd"]))
    pairs.sort(key=lambda row: -abs(float(row["correlation"])))
    return {
        "threshold": threshold,
        "underlyings_with_history": len(series),
        "aligned_observations": len(frame),
        "pairs": pairs,
        "clusters": clusters,
    }


def _drawdown(
    equity_curve: pd.Series | Iterable[float] | None,
) -> dict[str, Any] | None:
    if equity_curve is None:
        return None
    try:
        report = max_drawdown_analysis(equity_curve)
    except (TypeError, ValueError):
        return None
    return {
        "max_drawdown_pct": round(
            float(report.get("max_drawdown") or 0.0) * 100.0,
            4,
        ),
        "recovered": bool(report.get("recovered")),
        "peak_date": _json_value(report.get("peak_date")),
        "trough_date": _json_value(report.get("trough_date")),
        "recovery_date": _json_value(report.get("recovery_date")),
        "peak_to_trough_periods": report.get("peak_to_trough_periods"),
        "trough_to_recovery_periods": report.get("trough_to_recovery_periods"),
    }


def _parse_occ(symbol: str | None) -> dict[str, Any] | None:
    if not symbol:
        return None
    match = _OCC_RE.fullmatch(symbol.strip().upper())
    if not match:
        return None
    root, date_token, option_token, _ = match.groups()
    try:
        expiration = datetime.strptime(date_token, "%y%m%d").date().isoformat()
    except ValueError:
        return None
    return {
        "underlying": root,
        "expiration": expiration,
        "option_type": "call" if option_token == "C" else "put",
    }


def _expiration_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    text = str(value).strip()
    if not text:
        return ""
    try:
        return pd.Timestamp(value).date().isoformat()
    except (TypeError, ValueError):
        return text


def _loss_pct(pnl: float | None, equity: float) -> float | None:
    number = _finite(pnl)
    if number is None:
        return None
    return max(0.0, -number / equity * 100.0)


def _invalid_report(config: AccountRiskConfig, reason: str) -> dict[str, Any]:
    return {
        "approved": False,
        "trading_blocked": True,
        "decision": "ACCOUNT_RISK_BLOCKED",
        "blocking_reasons": [reason],
        "warnings": [],
        "config": asdict(config),
    }


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


def _abs_finite(value: object) -> float | None:
    number = _finite(value)
    return None if number is None else abs(number)


def _nonnegative_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number >= 0 else None


def _round_or_none(value: object, digits: int = 4) -> float | None:
    number = _finite(value)
    return None if number is None else round(number, digits)


def _json_value(value: object) -> Any:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _dedupe(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if str(value)))


__all__ = [
    "AccountRiskConfig",
    "assess_account_portfolio_risk",
    "risk_summary_from_report",
]
