"""Proposal-only exit management for long-premium option positions.

The module evaluates current bid-side liquidation value against a versioned exit
policy and may append an ``EXIT_PROPOSED`` journal event. It deliberately does
not import or call broker mutation functions. A separate approved execution seam
must translate a proposal into an order.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import math
import re
from typing import Any, Iterable, Mapping

from .journal import JournalEntry, JournalStage
from .models import PlatformEnvironment, PlatformEvent, SystemIdentity
from .store import TradingPlatformStore

UTC = timezone.utc
_OCC_RE = re.compile(r"^([A-Z0-9]{1,6})(\d{6})([CP])(\d{8})$")


@dataclass(frozen=True)
class ExitPolicyConfig:
    """Conservative defaults for long-option paper exit proposals."""

    max_quote_age_seconds: float = 90.0
    hard_stop_loss_pct: float = 50.0
    profit_take_pct: float = 100.0
    trailing_activation_gain_pct: float = 75.0
    trailing_drawdown_from_peak_pct: float = 25.0
    max_dte_to_hold: int = 3
    max_holding_days: int = 20

    def validate(self) -> None:
        if self.max_quote_age_seconds <= 0:
            raise ValueError("max_quote_age_seconds must be positive")
        if not 0 < self.hard_stop_loss_pct < 100:
            raise ValueError("hard_stop_loss_pct must be between 0 and 100")
        if self.profit_take_pct <= 0:
            raise ValueError("profit_take_pct must be positive")
        if self.trailing_activation_gain_pct < 0:
            raise ValueError("trailing_activation_gain_pct cannot be negative")
        if not 0 < self.trailing_drawdown_from_peak_pct < 100:
            raise ValueError("trailing_drawdown_from_peak_pct must be between 0 and 100")
        if self.max_dte_to_hold < 0:
            raise ValueError("max_dte_to_hold cannot be negative")
        if self.max_holding_days < 1:
            raise ValueError("max_holding_days must be at least 1")


def evaluate_long_option_exit(
    position: Mapping[str, Any],
    quote: Mapping[str, Any],
    *,
    now: datetime | None = None,
    config: ExitPolicyConfig | None = None,
) -> dict[str, Any]:
    """Evaluate one long option against the exit policy using current bid value.

    ``EXIT_PROPOSED`` is a decision record only. It is never an order.
    Missing/stale inputs fail closed to ``NO_ACTION`` rather than guessing.
    """
    cfg = config or ExitPolicyConfig()
    cfg.validate()
    observed_at = _aware_now(now)

    contract = str(
        position.get("contract_symbol") or position.get("symbol") or ""
    ).strip().upper().replace(" ", "")
    occ = _parse_occ(contract)
    underlying = str(
        position.get("underlying") or (occ or {}).get("underlying") or ""
    ).strip().upper()
    quantity = _positive_int(
        position.get("quantity")
        if position.get("quantity") is not None
        else position.get("qty")
    )
    side = str(position.get("side") or "long").strip().lower()
    entry_price = _positive(
        position.get("fill_price")
        or position.get("avg_entry_price")
        or position.get("entry_price")
        or position.get("entry_ask")
    )
    # Zero is a real executable-side observation for a long option. Do not use
    # boolean ``or`` selection here because ``0.0`` must survive normalization.
    bid = _nonnegative(_first_present(quote, "bid", "bid_price", "bp"))
    quote_time = _timestamp(
        _first_present(quote, "quote_time", "timestamp", "t")
    )

    blocking: list[str] = []
    if occ is None:
        blocking.append("valid_occ_contract_required")
    if side != "long":
        blocking.append("long_option_position_required")
    if quantity is None:
        blocking.append("positive_quantity_required")
    if entry_price is None:
        blocking.append("entry_fill_price_required")
    if bid is None:
        blocking.append("current_bid_required")
    if quote_time is None:
        blocking.append("quote_timestamp_required")

    quote_age_seconds = None
    if quote_time is not None:
        quote_age_seconds = max(0.0, (observed_at - quote_time).total_seconds())
        if quote_time > observed_at:
            blocking.append("future_quote_timestamp")
        elif quote_age_seconds > cfg.max_quote_age_seconds:
            blocking.append("stale_option_quote")

    if blocking:
        return {
            "decision": "NO_ACTION",
            "exit_proposed": False,
            "blocking_reasons": _dedupe(blocking),
            "exit_reasons": [],
            "contract_symbol": contract or None,
            "underlying": underlying or None,
            "observed_at": observed_at.isoformat(),
            "quote_age_seconds": _round(quote_age_seconds),
            "policy": asdict(cfg),
        }

    assert occ is not None
    assert quantity is not None
    assert entry_price is not None
    assert bid is not None

    return_pct = (bid / entry_price - 1.0) * 100.0
    high_watermark_bid = _positive(position.get("high_watermark_bid"))
    high_watermark_bid = max(entry_price, bid, high_watermark_bid or 0.0)
    peak_gain_pct = (high_watermark_bid / entry_price - 1.0) * 100.0
    drawdown_from_peak_pct = (
        0.0 if high_watermark_bid <= 0 else (1.0 - bid / high_watermark_bid) * 100.0
    )

    expiration = _date(position.get("expiration")) or occ["expiration"]
    dte = (expiration - observed_at.date()).days
    filled_at = _timestamp(
        position.get("filled_at") or position.get("entry_time") or position.get("opened_at")
    )
    holding_days = None
    if filled_at is not None:
        holding_days = max(0, (observed_at.date() - filled_at.date()).days)

    thesis_valid = _optional_bool(position.get("thesis_valid"))
    account_risk_exit = bool(position.get("account_risk_exit_required"))
    reasons: list[str] = []

    # Risk/thesis exits outrank price targets. The policy only proposes; it does
    # not infer that a stale or missing quote is safe to trade against.
    if account_risk_exit:
        reasons.append("account_risk_exit_required")
    if thesis_valid is False:
        reasons.append("thesis_invalidated")
    if return_pct <= -cfg.hard_stop_loss_pct:
        reasons.append("hard_stop_loss")
    if dte <= cfg.max_dte_to_hold:
        reasons.append("time_stop_dte")
    if holding_days is not None and holding_days >= cfg.max_holding_days:
        reasons.append("time_stop_holding_period")
    if return_pct >= cfg.profit_take_pct:
        reasons.append("profit_take_target")
    if (
        peak_gain_pct >= cfg.trailing_activation_gain_pct
        and drawdown_from_peak_pct >= cfg.trailing_drawdown_from_peak_pct
    ):
        reasons.append("trailing_profit_protection")

    exit_proposed = bool(reasons)
    multiplier = _positive(position.get("multiplier")) or 100.0
    market_value_usd = bid * multiplier * quantity
    entry_cost_usd = entry_price * multiplier * quantity
    unrealized_pnl_usd = market_value_usd - entry_cost_usd

    return {
        "decision": "EXIT_PROPOSED" if exit_proposed else "HOLD",
        "exit_proposed": exit_proposed,
        "blocking_reasons": [],
        "exit_reasons": _dedupe(reasons),
        "contract_symbol": contract,
        "underlying": underlying or occ["underlying"],
        "option_type": occ["option_type"],
        "expiration": expiration.isoformat(),
        "quantity": quantity,
        "entry_price": round(entry_price, 6),
        "current_bid": round(bid, 6),
        "return_pct": round(return_pct, 4),
        "entry_cost_usd": round(entry_cost_usd, 2),
        "market_value_usd": round(market_value_usd, 2),
        "unrealized_pnl_usd": round(unrealized_pnl_usd, 2),
        "high_watermark_bid": round(high_watermark_bid, 6),
        "peak_gain_pct": round(peak_gain_pct, 4),
        "drawdown_from_peak_pct": round(drawdown_from_peak_pct, 4),
        "dte": dte,
        "holding_days": holding_days,
        "quote_age_seconds": _round(quote_age_seconds),
        "observed_at": observed_at.isoformat(),
        "policy": asdict(cfg),
        "warning": "Exit proposal only; no broker order was created or submitted.",
    }


def scan_long_option_exits(
    positions: Iterable[Mapping[str, Any]],
    quotes_by_contract: Mapping[str, Mapping[str, Any]],
    *,
    now: datetime | None = None,
    config: ExitPolicyConfig | None = None,
    store: TradingPlatformStore | None = None,
    system: SystemIdentity | None = None,
    snapshot_id: str | None = None,
) -> dict[str, Any]:
    """Evaluate a position set and optionally append proposal journal records."""
    cfg = config or ExitPolicyConfig()
    cfg.validate()
    observed_at = _aware_now(now)
    identity = system or SystemIdentity()

    results: list[dict[str, Any]] = []
    proposed = 0
    blocked = 0
    for raw in positions:
        contract = str(
            raw.get("contract_symbol") or raw.get("symbol") or ""
        ).strip().upper().replace(" ", "")
        quote = quotes_by_contract.get(contract) or {}
        result = evaluate_long_option_exit(raw, quote, now=observed_at, config=cfg)
        results.append(result)
        if result["exit_proposed"]:
            proposed += 1
            if store is not None:
                _append_exit_proposal(
                    raw,
                    result,
                    store=store,
                    system=identity,
                    snapshot_id=snapshot_id,
                )
        elif result["decision"] == "NO_ACTION":
            blocked += 1

    return {
        "status": "ok" if blocked == 0 else "degraded",
        "observed_at": observed_at.isoformat(),
        "positions_evaluated": len(results),
        "exit_proposals": proposed,
        "blocked_no_action": blocked,
        "results": results,
        "policy": asdict(cfg),
        "broker_mutation": False,
    }


def _append_exit_proposal(
    position: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    store: TradingPlatformStore,
    system: SystemIdentity,
    snapshot_id: str | None,
) -> None:
    contract = str(result.get("contract_symbol") or "").strip().upper()
    entry = JournalEntry(
        stage=JournalStage.EXIT_PROPOSED,
        environment=PlatformEnvironment.PAPER,
        system=system,
        snapshot_id=snapshot_id,
        symbol=str(result.get("underlying") or "UNKNOWN").strip().upper(),
        contract_symbol=contract or None,
        option_type=_text(result.get("option_type")),
        quantity=_positive_int(result.get("quantity")),
        fill_price=_nonnegative(result.get("entry_price")),
        hard_reasons=[str(item) for item in result.get("exit_reasons", [])],
        broker_order_id=_text(position.get("broker_order_id")),
        metadata={
            "exit_proposal": dict(result),
            "source_position": {
                "filled_at": position.get("filled_at"),
                "high_watermark_bid": position.get("high_watermark_bid"),
            },
            "broker_mutation": False,
        },
    )
    store.append_journal_entry(entry)
    store.append_event(
        PlatformEvent(
            event_type="paper_exit_proposed",
            environment=PlatformEnvironment.PAPER,
            system=system,
            payload={
                "journal_id": entry.journal_id,
                "snapshot_id": snapshot_id,
                "contract_symbol": contract or None,
                "underlying": entry.symbol,
                "exit_reasons": list(entry.hard_reasons),
                "broker_mutation": False,
            },
        )
    )


def _parse_occ(value: str) -> dict[str, Any] | None:
    match = _OCC_RE.fullmatch(value)
    if not match:
        return None
    root, date_token, side, _strike = match.groups()
    try:
        expiration = datetime.strptime(date_token, "%y%m%d").date()
    except ValueError:
        return None
    return {
        "underlying": root,
        "expiration": expiration,
        "option_type": "call" if side == "C" else "put",
    }


def _date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _aware_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(UTC)


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _positive(value: object) -> float | None:
    number = _number(value)
    return number if number is not None and number > 0 else None


def _nonnegative(value: object) -> float | None:
    number = _number(value)
    return number if number is not None and number >= 0 else None


def _positive_int(value: object) -> int | None:
    number = _number(value)
    if number is None or number <= 0 or not float(number).is_integer():
        return None
    return int(number)


def _first_present(mapping: Mapping[str, Any], *keys: str) -> object:
    for key in keys:
        if key in mapping and mapping.get(key) is not None:
            return mapping.get(key)
    return None


def _optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    token = str(value).strip().lower()
    if token in {"true", "1", "yes"}:
        return True
    if token in {"false", "0", "no"}:
        return False
    return None


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def _text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


__all__ = ["ExitPolicyConfig", "evaluate_long_option_exit", "scan_long_option_exits"]
