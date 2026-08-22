"""Defined-risk portfolio gates for long-premium option candidates.

This layer does not size a user's real account.  It provides deterministic
research/paper controls that require explicit account-equity and open-risk inputs.
Long calls/puts are treated as losing at most the premium paid; short/naked option
risk is deliberately unsupported here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping
import math


@dataclass(frozen=True)
class PortfolioRiskConfig:
    """Concentration limits expressed as percentages of supplied equity."""

    max_risk_per_trade_pct: float = 1.0
    max_total_open_premium_risk_pct: float = 5.0
    max_same_underlying_risk_pct: float = 1.5
    max_same_expiry_risk_pct: float = 2.5
    max_same_theme_risk_pct: float = 2.5
    max_positions: int = 6
    require_positive_ev: bool = True
    require_walk_forward_pass: bool = True

    def validate(self) -> None:
        for name, value in (
            ("max_risk_per_trade_pct", self.max_risk_per_trade_pct),
            ("max_total_open_premium_risk_pct", self.max_total_open_premium_risk_pct),
            ("max_same_underlying_risk_pct", self.max_same_underlying_risk_pct),
            ("max_same_expiry_risk_pct", self.max_same_expiry_risk_pct),
            ("max_same_theme_risk_pct", self.max_same_theme_risk_pct),
        ):
            if not 0 < value <= 100:
                raise ValueError(f"{name} must be > 0 and <= 100")
        if self.max_positions < 1:
            raise ValueError("max_positions must be at least 1")


def assess_portfolio_risk(
    candidate: Mapping[str, Any],
    *,
    account_equity_usd: float,
    quantity: int = 1,
    existing_positions: Iterable[Mapping[str, Any]] = (),
    ev_report: Mapping[str, Any] | None = None,
    walk_forward_report: Mapping[str, Any] | None = None,
    config: PortfolioRiskConfig | None = None,
) -> dict[str, Any]:
    """Fail closed unless a long-premium candidate fits every risk bucket."""
    cfg = config or PortfolioRiskConfig()
    cfg.validate()
    equity = _positive(account_equity_usd)
    if equity is None:
        return _reject(cfg, ["account_equity_usd must be a positive finite number"])
    if isinstance(quantity, bool) or int(quantity) != quantity or int(quantity) < 1:
        return _reject(cfg, ["quantity must be a positive whole number"])
    quantity = int(quantity)

    option_type = str(candidate.get("option_type") or "").lower()
    if option_type not in {"call", "put"}:
        return _reject(cfg, ["only defined-risk long calls/puts are supported"])

    side = str(candidate.get("side") or "buy").lower()
    if side != "buy":
        return _reject(cfg, ["short/naked option exposure is outside this risk engine"])

    max_loss = _positive(candidate.get("max_loss_usd"))
    if max_loss is None:
        ask = _positive(candidate.get("entry_ask"))
        if ask is not None:
            max_loss = ask * 100.0
    if max_loss is None:
        return _reject(cfg, ["candidate must provide max_loss_usd or a positive entry_ask"])

    proposed_risk = max_loss * quantity
    existing = [_normalize_position(row) for row in existing_positions]
    existing = [row for row in existing if row is not None]
    total_existing = sum(row["risk_usd"] for row in existing)

    symbol = str(candidate.get("symbol") or candidate.get("ticker") or "").upper()
    underlying = str(candidate.get("underlying") or symbol).upper()
    expiration = str(candidate.get("expiration") or "")
    theme = str(candidate.get("theme") or candidate.get("sector") or "").strip().lower()

    same_underlying = sum(row["risk_usd"] for row in existing if row["underlying"] == underlying)
    same_expiry = sum(row["risk_usd"] for row in existing if expiration and row["expiration"] == expiration)
    same_theme = sum(row["risk_usd"] for row in existing if theme and row["theme"] == theme)

    reasons: list[str] = []
    trade_pct = proposed_risk / equity * 100.0
    total_pct = (total_existing + proposed_risk) / equity * 100.0
    underlying_pct = (same_underlying + proposed_risk) / equity * 100.0
    expiry_pct = (same_expiry + proposed_risk) / equity * 100.0
    theme_pct = (same_theme + proposed_risk) / equity * 100.0

    if trade_pct > cfg.max_risk_per_trade_pct:
        reasons.append("single_trade_risk_limit")
    if total_pct > cfg.max_total_open_premium_risk_pct:
        reasons.append("total_open_premium_risk_limit")
    if underlying_pct > cfg.max_same_underlying_risk_pct:
        reasons.append("same_underlying_concentration")
    if expiration and expiry_pct > cfg.max_same_expiry_risk_pct:
        reasons.append("same_expiry_concentration")
    if theme and theme_pct > cfg.max_same_theme_risk_pct:
        reasons.append("same_theme_concentration")
    if len(existing) + 1 > cfg.max_positions:
        reasons.append("max_positions")
    if cfg.require_positive_ev and not bool((ev_report or {}).get("positive_ev")):
        reasons.append("positive_ev_required")
    if cfg.require_walk_forward_pass and (walk_forward_report or {}).get("decision") != "WALK_FORWARD_PASS":
        reasons.append("walk_forward_pass_required")

    approved = not reasons
    return {
        "approved": approved,
        "decision": "RISK_APPROVED" if approved else "RISK_REJECTED",
        "reasons": reasons,
        "quantity": quantity,
        "proposed_max_loss_usd": round(proposed_risk, 2),
        "risk_percentages": {
            "single_trade": round(trade_pct, 4),
            "total_open_after_trade": round(total_pct, 4),
            "same_underlying_after_trade": round(underlying_pct, 4),
            "same_expiry_after_trade": round(expiry_pct, 4) if expiration else None,
            "same_theme_after_trade": round(theme_pct, 4) if theme else None,
        },
        "existing_position_count": len(existing),
        "config": asdict(cfg),
        "warning": (
            "Research/paper risk gate only. Supplied equity and positions determine the result; this is not personalized "
            "financial advice or a guarantee against loss."
        ),
    }


def _normalize_position(value: Mapping[str, Any]) -> dict[str, Any] | None:
    risk = _positive(value.get("risk_usd") or value.get("max_loss_usd"))
    if risk is None:
        return None
    quantity = _positive(value.get("quantity") or 1.0) or 1.0
    return {
        "risk_usd": risk * quantity,
        "underlying": str(value.get("underlying") or value.get("symbol") or "").upper(),
        "expiration": str(value.get("expiration") or ""),
        "theme": str(value.get("theme") or value.get("sector") or "").strip().lower(),
    }


def _positive(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _reject(config: PortfolioRiskConfig, reasons: list[str]) -> dict[str, Any]:
    return {
        "approved": False,
        "decision": "RISK_REJECTED",
        "reasons": reasons,
        "config": asdict(config),
    }


__all__ = ["PortfolioRiskConfig", "assess_portfolio_risk"]
