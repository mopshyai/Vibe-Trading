"""Fail-closed Alpaca paper execution for selected long-option candidates.

This module is deliberately narrower than the general Alpaca connector:
- paper profile only;
- OCC/OSI option symbols only;
- long premium (buy) only;
- integer contracts only;
- limit + DAY orders only;
- explicit EV, walk-forward and portfolio-risk approvals by default;
- dry-run by default.

It is intended as the final research-to-paper seam.  It cannot place a live
order even if a live Alpaca config is supplied.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import re
from typing import Any, Mapping

from src.trading.connectors.alpaca import sdk as alpaca_sdk


_OCC_RE = re.compile(r"^(?P<root>[A-Z0-9]{1,6})(?P<date>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")


@dataclass(frozen=True)
class PaperExecutionConfig:
    """Safety controls for the research-to-paper bridge."""

    dry_run: bool = True
    max_contracts: int = 1
    max_premium_risk_usd: float = 500.0
    max_spread_pct: float = 15.0
    max_limit_above_reference_ask_pct: float = 2.0
    require_ev_pass: bool = True
    require_walk_forward_pass: bool = True
    require_risk_approval: bool = True
    allow_queue_when_market_closed: bool = False

    def validate(self) -> None:
        if self.max_contracts < 1:
            raise ValueError("max_contracts must be at least 1")
        if self.max_premium_risk_usd <= 0:
            raise ValueError("max_premium_risk_usd must be positive")
        if not 0 < self.max_spread_pct <= 100:
            raise ValueError("max_spread_pct must be > 0 and <= 100")
        if not 0 <= self.max_limit_above_reference_ask_pct <= 25:
            raise ValueError("max_limit_above_reference_ask_pct must be between 0 and 25")


def build_paper_option_order(
    candidate: Mapping[str, Any],
    *,
    quantity: int = 1,
    bid: float | None = None,
    ask: float | None = None,
    limit_price: float | None = None,
    ev_report: Mapping[str, Any] | None = None,
    walk_forward_report: Mapping[str, Any] | None = None,
    risk_report: Mapping[str, Any] | None = None,
    config: PaperExecutionConfig | None = None,
) -> dict[str, Any]:
    """Construct and validate one long-option Alpaca paper order."""
    cfg = config or PaperExecutionConfig()
    cfg.validate()
    reasons: list[str] = []

    contract = str(candidate.get("contract_symbol") or "").strip().upper().replace(" ", "")
    match = _OCC_RE.fullmatch(contract)
    if match is None:
        reasons.append("valid_OCC_option_contract_required")

    option_type = str(candidate.get("option_type") or "").strip().lower()
    if match is not None:
        parsed_type = "call" if match.group("cp") == "C" else "put"
        if option_type and option_type != parsed_type:
            reasons.append("candidate_option_type_mismatch")
        option_type = parsed_type
    if option_type not in {"call", "put"}:
        reasons.append("long_call_or_put_required")

    if isinstance(quantity, bool) or int(quantity) != quantity or int(quantity) < 1:
        reasons.append("quantity_must_be_positive_integer_contracts")
        quantity_value = 0
    else:
        quantity_value = int(quantity)
        if quantity_value > cfg.max_contracts:
            reasons.append("max_contracts_exceeded")

    reference_bid = _price(bid if bid is not None else candidate.get("bid"))
    reference_ask = _price(ask if ask is not None else candidate.get("entry_ask"))
    if reference_ask is None:
        reasons.append("positive_reference_ask_required")

    spread_pct = None
    if reference_bid is not None and reference_ask is not None:
        if reference_bid > reference_ask:
            reasons.append("crossed_option_market")
        else:
            midpoint = (reference_bid + reference_ask) / 2.0
            spread_pct = (reference_ask - reference_bid) / midpoint * 100.0 if midpoint > 0 else math.inf
            if spread_pct > cfg.max_spread_pct:
                reasons.append("spread_too_wide")

    requested_limit = _price(limit_price)
    if requested_limit is None:
        requested_limit = reference_ask
    if requested_limit is None:
        reasons.append("limit_price_required")
    elif reference_ask is not None:
        max_allowed_limit = reference_ask * (1.0 + cfg.max_limit_above_reference_ask_pct / 100.0)
        if requested_limit > max_allowed_limit + 1e-9:
            reasons.append("limit_price_chases_reference_ask")

    max_debit = None
    if requested_limit is not None and quantity_value > 0:
        max_debit = requested_limit * 100.0 * quantity_value
        if max_debit > cfg.max_premium_risk_usd:
            reasons.append("paper_premium_risk_cap_exceeded")

    if cfg.require_ev_pass and not bool((ev_report or {}).get("positive_ev")):
        reasons.append("positive_ev_gate_required")
    if cfg.require_walk_forward_pass and (walk_forward_report or {}).get("decision") != "WALK_FORWARD_PASS":
        reasons.append("walk_forward_pass_required")
    if cfg.require_risk_approval and not bool((risk_report or {}).get("approved")):
        reasons.append("portfolio_risk_approval_required")

    ready = not reasons
    target_profit_pct = _number(candidate.get("target_profit_pct"), 300.0)
    target_multiple = 1.0 + target_profit_pct / 100.0
    target_premium = requested_limit * target_multiple if requested_limit is not None else None

    return {
        "ready": ready,
        "decision": "PAPER_ORDER_READY" if ready else "PAPER_ORDER_REJECTED",
        "reasons": reasons,
        "order": {
            "symbol": contract or None,
            "underlying": match.group("root") if match is not None else None,
            "option_type": option_type or None,
            "side": "buy",
            "quantity": quantity_value,
            "order_type": "limit",
            "limit_price": round(requested_limit, 4) if requested_limit is not None else None,
            "time_in_force": "day",
        },
        "reference_market": {
            "bid": reference_bid,
            "ask": reference_ask,
            "spread_pct": None if spread_pct is None or not math.isfinite(spread_pct) else round(spread_pct, 4),
        },
        "max_premium_risk_usd": None if max_debit is None else round(max_debit, 2),
        "target_profit_pct": target_profit_pct,
        "target_multiple": round(target_multiple, 4),
        "target_premium": None if target_premium is None else round(target_premium, 4),
        "dry_run": cfg.dry_run,
        "config": asdict(cfg),
        "warning": "Paper execution only. No live profile is permitted by submit_paper_option_order.",
    }


def submit_paper_option_order(
    candidate: Mapping[str, Any],
    *,
    quantity: int = 1,
    bid: float | None = None,
    ask: float | None = None,
    limit_price: float | None = None,
    market_is_open: bool = False,
    ev_report: Mapping[str, Any] | None = None,
    walk_forward_report: Mapping[str, Any] | None = None,
    risk_report: Mapping[str, Any] | None = None,
    alpaca_config: alpaca_sdk.AlpacaConfig | None = None,
    config: PaperExecutionConfig | None = None,
) -> dict[str, Any]:
    """Submit one validated long option to Alpaca **paper only**.

    ``dry_run`` defaults to true.  A caller that intentionally wants a paper
    broker mutation must construct ``PaperExecutionConfig(dry_run=False)``.
    Even then, a non-paper Alpaca profile is rejected before the connector's
    order function is reached.
    """
    cfg = config or PaperExecutionConfig()
    plan = build_paper_option_order(
        candidate,
        quantity=quantity,
        bid=bid,
        ask=ask,
        limit_price=limit_price,
        ev_report=ev_report,
        walk_forward_report=walk_forward_report,
        risk_report=risk_report,
        config=cfg,
    )
    if not plan["ready"]:
        return plan

    broker_cfg = alpaca_config or alpaca_sdk.load_config()
    if not broker_cfg.is_paper or broker_cfg.profile != "paper":
        return {
            **plan,
            "ready": False,
            "decision": "PAPER_ORDER_REJECTED",
            "reasons": [*plan["reasons"], "alpaca_paper_profile_required"],
        }

    if not market_is_open and not cfg.allow_queue_when_market_closed:
        return {
            **plan,
            "ready": False,
            "decision": "PAPER_ORDER_REJECTED",
            "reasons": [*plan["reasons"], "market_open_confirmation_required"],
        }

    if cfg.dry_run:
        return {**plan, "decision": "PAPER_ORDER_DRY_RUN", "submitted": False}

    order = plan["order"]
    result = alpaca_sdk.place_order(
        broker_cfg,
        symbol=str(order["symbol"]),
        side="buy",
        quantity=int(order["quantity"]),
        order_type="limit",
        limit_price=float(order["limit_price"]),
        time_in_force="day",
    )
    if result.get("status") != "ok" or not result.get("is_paper"):
        return {
            **plan,
            "decision": "PAPER_ORDER_SUBMISSION_FAILED",
            "submitted": False,
            "broker_result": result,
        }
    return {
        **plan,
        "decision": "PAPER_ORDER_SUBMITTED",
        "submitted": True,
        "broker_result": result,
    }


def _price(value: object) -> float | None:
    number = _finite(value)
    if number is None or number <= 0:
        return None
    return number


def _number(value: object, default: float) -> float:
    number = _finite(value)
    return default if number is None else number


def _finite(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


__all__ = [
    "PaperExecutionConfig",
    "build_paper_option_order",
    "submit_paper_option_order",
]
