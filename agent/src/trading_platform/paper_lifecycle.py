"""Supervised Alpaca paper-option lifecycle for the Trading Desk.

This module turns an already-approved research candidate into an operational
paper workflow without weakening the existing execution guards:

1. verify the configured broker profile is Alpaca paper;
2. read the broker market clock instead of trusting a caller supplied flag;
3. reprice the exact option contract from a current Alpaca option snapshot;
4. enforce quote freshness and OPRA for actual paper submission;
5. re-run the existing EV, walk-forward and portfolio-risk order gates;
6. optionally submit one long-premium limit order to the paper account;
7. append proposed/submitted/fill/error states to the Trading Platform journal;
8. reconcile submitted paper orders later through read-only order-status checks.

No live Alpaca profile can pass this seam. A submission also requires the caller
to set both ``submit=True`` and ``confirm_submit=True``. The default is a dry-run
proposal.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import math
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlencode

import requests

from src.options_market.paper_execution import PaperExecutionConfig, submit_paper_option_order
from src.trading import tap_forward
from src.trading.connectors.alpaca import sdk as alpaca_sdk

from .journal import JournalEntry, JournalStage
from .models import DeskDecision, PlatformEnvironment, PlatformEvent, SystemIdentity
from .store import TradingPlatformStore

UTC = timezone.utc


@dataclass(frozen=True)
class PaperLifecycleConfig:
    option_feed: str = "opra"
    max_quote_age_seconds: float = 90.0
    max_contracts: int = 1
    max_premium_risk_usd: float = 500.0
    max_spread_pct: float = 15.0
    max_limit_above_reference_ask_pct: float = 2.0

    def validate(self) -> None:
        if self.option_feed not in {"opra", "indicative"}:
            raise ValueError("option_feed must be 'opra' or 'indicative'")
        if self.max_quote_age_seconds <= 0:
            raise ValueError("max_quote_age_seconds must be positive")
        if self.max_contracts < 1:
            raise ValueError("max_contracts must be at least 1")
        if self.max_premium_risk_usd <= 0:
            raise ValueError("max_premium_risk_usd must be positive")
        if not 0 < self.max_spread_pct <= 100:
            raise ValueError("max_spread_pct must be > 0 and <= 100")
        if not 0 <= self.max_limit_above_reference_ask_pct <= 25:
            raise ValueError("max_limit_above_reference_ask_pct must be between 0 and 25")


def run_paper_option_lifecycle(
    candidate: Mapping[str, Any],
    *,
    ev_report: Mapping[str, Any],
    walk_forward_report: Mapping[str, Any],
    risk_report: Mapping[str, Any],
    quantity: int = 1,
    limit_price: float | None = None,
    submit: bool = False,
    confirm_submit: bool = False,
    alpaca_config: alpaca_sdk.AlpacaConfig | None = None,
    config: PaperLifecycleConfig | None = None,
    store: TradingPlatformStore | None = None,
    system: SystemIdentity | None = None,
    snapshot_id: str | None = None,
    session: requests.Session | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Prepare or submit one option to Alpaca paper with fresh runtime checks."""
    cfg = config or PaperLifecycleConfig()
    cfg.validate()
    broker_cfg = alpaca_config or alpaca_sdk.load_config()
    identity = system or SystemIdentity()
    observed_at = _aware_now(now)

    contract = str(candidate.get("contract_symbol") or "").strip().upper().replace(" ", "")
    underlying = str(candidate.get("symbol") or candidate.get("underlying") or "").strip().upper()
    if not underlying and contract:
        underlying = _occ_root(contract)

    runtime_reasons: list[str] = []
    if not broker_cfg.is_paper or broker_cfg.profile != "paper":
        runtime_reasons.append("alpaca_paper_profile_required")
    if submit and not confirm_submit:
        runtime_reasons.append("explicit_paper_submit_confirmation_required")
    if submit and cfg.option_feed != "opra":
        runtime_reasons.append("opra_required_for_paper_submission")

    clock: dict[str, Any] | None = None
    quote: dict[str, Any] | None = None
    if not runtime_reasons:
        try:
            clock = fetch_alpaca_market_clock(broker_cfg, session=session)
        except Exception as exc:  # noqa: BLE001 - runtime gate fails closed
            runtime_reasons.append(f"market_clock_unavailable:{type(exc).__name__}")
        try:
            quote = fetch_current_option_quote(
                contract,
                broker_cfg,
                feed=cfg.option_feed,
                session=session,
                now=observed_at,
            )
        except Exception as exc:  # noqa: BLE001 - runtime gate fails closed
            runtime_reasons.append(f"option_quote_unavailable:{type(exc).__name__}")

    if quote is not None:
        age = quote.get("age_seconds")
        if age is None:
            runtime_reasons.append("option_quote_timestamp_required")
        elif float(age) > cfg.max_quote_age_seconds:
            runtime_reasons.append("option_quote_stale")
        if quote.get("ask") is None:
            runtime_reasons.append("option_ask_required")

    if runtime_reasons:
        result = {
            "ready": False,
            "submitted": False,
            "decision": "PAPER_RUNTIME_REJECTED",
            "reasons": _dedupe(runtime_reasons),
            "contract_symbol": contract or None,
            "underlying": underlying or None,
            "clock": clock,
            "quote": quote,
            "observed_at": observed_at.isoformat(),
            "config": asdict(cfg),
        }
        _journal_result(
            result,
            candidate=candidate,
            quantity=quantity,
            store=store,
            system=identity,
            snapshot_id=snapshot_id,
            stage=JournalStage.REJECTED,
        )
        return result

    assert clock is not None
    assert quote is not None
    execution_cfg = PaperExecutionConfig(
        dry_run=not submit,
        max_contracts=cfg.max_contracts,
        max_premium_risk_usd=cfg.max_premium_risk_usd,
        max_spread_pct=cfg.max_spread_pct,
        max_limit_above_reference_ask_pct=cfg.max_limit_above_reference_ask_pct,
        require_ev_pass=True,
        require_walk_forward_pass=True,
        require_risk_approval=True,
        allow_queue_when_market_closed=False,
    )
    execution = submit_paper_option_order(
        candidate,
        quantity=quantity,
        bid=quote.get("bid"),
        ask=quote.get("ask"),
        limit_price=limit_price,
        market_is_open=bool(clock.get("is_open")),
        ev_report=ev_report,
        walk_forward_report=walk_forward_report,
        risk_report=risk_report,
        alpaca_config=broker_cfg,
        config=execution_cfg,
    )
    result = {
        **execution,
        "clock": clock,
        "quote": quote,
        "option_feed": cfg.option_feed,
        "observed_at": observed_at.isoformat(),
        "runtime_config": asdict(cfg),
    }

    if result.get("submitted"):
        stage = JournalStage.SUBMITTED
    elif result.get("decision") == "PAPER_ORDER_DRY_RUN" and result.get("ready"):
        stage = JournalStage.PROPOSED
    elif result.get("ready"):
        stage = JournalStage.APPROVED
    else:
        stage = JournalStage.REJECTED
    _journal_result(
        result,
        candidate=candidate,
        quantity=quantity,
        store=store,
        system=identity,
        snapshot_id=snapshot_id,
        stage=stage,
    )

    if result.get("submitted"):
        broker_result = result.get("broker_result") if isinstance(result.get("broker_result"), Mapping) else {}
        order_id = str(broker_result.get("order_id") or "").strip()
        if order_id:
            try:
                status = fetch_alpaca_order(order_id, broker_cfg, session=session)
            except Exception as exc:  # noqa: BLE001
                result["post_submit_status_error"] = f"{type(exc).__name__}: {exc}"
            else:
                result["post_submit_status"] = status
                append_reconciled_order_status(
                    status,
                    store=store,
                    system=identity,
                    snapshot_id=snapshot_id,
                    prior_candidate=candidate,
                )
    return result


def sync_paper_order_lifecycle(
    *,
    store: TradingPlatformStore,
    alpaca_config: alpaca_sdk.AlpacaConfig | None = None,
    system: SystemIdentity | None = None,
    session: requests.Session | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """Reconcile submitted paper orders from the append-only journal."""
    broker_cfg = alpaca_config or alpaca_sdk.load_config()
    if not broker_cfg.is_paper or broker_cfg.profile != "paper":
        return {"status": "error", "reason": "alpaca_paper_profile_required", "synced": []}
    identity = system or SystemIdentity()
    rows = store.recent_journal(limit=limit)
    latest_by_order: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        order_id = str(row.get("broker_order_id") or "").strip()
        if order_id and order_id not in latest_by_order:
            latest_by_order[order_id] = row

    synced: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for order_id, prior in latest_by_order.items():
        try:
            status = fetch_alpaca_order(order_id, broker_cfg, session=session)
            appended = append_reconciled_order_status(
                status,
                store=store,
                system=identity,
                snapshot_id=str(prior.get("snapshot_id") or "").strip() or None,
                prior_candidate=prior,
                prior_stage=str(prior.get("stage") or "").strip(),
            )
            synced.append({"order_id": order_id, "status": status.get("status"), "journal_appended": appended})
        except Exception as exc:  # noqa: BLE001
            errors.append({"order_id": order_id, "error": f"{type(exc).__name__}: {exc}"})
    return {"status": "ok" if not errors else "partial", "synced": synced, "errors": errors}


def fetch_alpaca_market_clock(
    config: alpaca_sdk.AlpacaConfig,
    *,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    payload = _get_json(f"{config.host}/v2/clock", config, session=session)
    return {
        "is_open": bool(payload.get("is_open")),
        "timestamp": _text(payload.get("timestamp")),
        "next_open": _text(payload.get("next_open")),
        "next_close": _text(payload.get("next_close")),
    }


def fetch_current_option_quote(
    contract_symbol: str,
    config: alpaca_sdk.AlpacaConfig,
    *,
    feed: str = "opra",
    session: requests.Session | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    clean = str(contract_symbol or "").strip().upper().replace(" ", "")
    if not clean:
        raise ValueError("contract_symbol is required")
    if feed not in {"opra", "indicative"}:
        raise ValueError("feed must be opra or indicative")
    query = urlencode({"symbols": clean, "feed": feed, "limit": 1})
    payload = _get_json(
        f"{alpaca_sdk.DATA_HOST}/v1beta1/options/snapshots?{query}",
        config,
        session=session,
    )
    snapshots = payload.get("snapshots") if isinstance(payload, Mapping) else None
    row = snapshots.get(clean) if isinstance(snapshots, Mapping) else None
    if not isinstance(row, Mapping):
        raise RuntimeError("option snapshot missing for requested contract")
    latest = row.get("latestQuote") or row.get("latest_quote") or row.get("quote")
    if not isinstance(latest, Mapping):
        raise RuntimeError("latest option quote missing")
    bid = _nonnegative(latest.get("bp") or latest.get("bid_price") or latest.get("bid"))
    ask = _positive(latest.get("ap") or latest.get("ask_price") or latest.get("ask"))
    quote_time = _timestamp(latest.get("t") or latest.get("timestamp") or latest.get("time"))
    observed_at = _aware_now(now)
    age = None if quote_time is None else max(0.0, (observed_at - quote_time).total_seconds())
    return {
        "contract_symbol": clean,
        "feed": feed,
        "execution_grade_feed": feed == "opra",
        "bid": bid,
        "ask": ask,
        "bid_size": _nonnegative(latest.get("bs") or latest.get("bid_size")),
        "ask_size": _nonnegative(latest.get("as") or latest.get("ask_size")),
        "quote_time": quote_time.isoformat() if quote_time else None,
        "age_seconds": None if age is None else round(age, 3),
        "implied_volatility": _positive(row.get("impliedVolatility") or row.get("implied_volatility")),
        "greeks": dict(row.get("greeks")) if isinstance(row.get("greeks"), Mapping) else {},
    }


def fetch_alpaca_order(
    order_id: str,
    config: alpaca_sdk.AlpacaConfig,
    *,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    clean = str(order_id or "").strip()
    if not clean:
        raise ValueError("order_id is required")
    payload = _get_json(f"{config.host}/v2/orders/{clean}", config, session=session)
    return {
        "order_id": clean,
        "symbol": _text(payload.get("symbol")),
        "side": _text(payload.get("side")),
        "status": str(payload.get("status") or "").strip().lower(),
        "quantity": _number(payload.get("qty")),
        "filled_qty": _number(payload.get("filled_qty")),
        "filled_avg_price": _number(payload.get("filled_avg_price")),
        "limit_price": _number(payload.get("limit_price")),
        "submitted_at": _text(payload.get("submitted_at")),
        "filled_at": _text(payload.get("filled_at")),
        "canceled_at": _text(payload.get("canceled_at")),
        "expired_at": _text(payload.get("expired_at")),
    }


def append_reconciled_order_status(
    status: Mapping[str, Any],
    *,
    store: TradingPlatformStore | None,
    system: SystemIdentity,
    snapshot_id: str | None,
    prior_candidate: Mapping[str, Any],
    prior_stage: str | None = None,
) -> bool:
    if store is None:
        return False
    broker_status = str(status.get("status") or "").strip().lower()
    stage = _stage_from_broker_status(broker_status)
    if stage is None:
        return False
    if prior_stage and prior_stage == stage.value:
        return False
    contract = str(status.get("symbol") or prior_candidate.get("contract_symbol") or "").strip().upper()
    underlying = str(prior_candidate.get("symbol") or prior_candidate.get("underlying") or _occ_root(contract)).strip().upper()
    order_id = str(status.get("order_id") or prior_candidate.get("broker_order_id") or "").strip() or None
    filled_qty = _nonnegative_int(status.get("filled_qty"))
    entry = JournalEntry(
        stage=stage,
        environment=PlatformEnvironment.PAPER,
        system=system,
        snapshot_id=snapshot_id,
        symbol=underlying or _occ_root(contract) or "UNKNOWN",
        contract_symbol=contract or None,
        decision=DeskDecision.TRADE_READY_RESEARCH,
        quantity=filled_qty,
        broker_order_id=order_id,
        fill_price=_nonnegative(status.get("filled_avg_price")),
        metadata={"broker_status": broker_status, "broker_order": dict(status)},
    )
    store.append_journal_entry(entry)
    store.append_event(
        PlatformEvent(
            event_type="paper_order_reconciled",
            environment=PlatformEnvironment.PAPER,
            system=system,
            payload={
                "journal_id": entry.journal_id,
                "order_id": order_id,
                "contract_symbol": contract or None,
                "stage": stage.value,
                "broker_status": broker_status,
            },
        )
    )
    return True


def _journal_result(
    result: Mapping[str, Any],
    *,
    candidate: Mapping[str, Any],
    quantity: int,
    store: TradingPlatformStore | None,
    system: SystemIdentity,
    snapshot_id: str | None,
    stage: JournalStage,
) -> None:
    if store is None:
        return
    order = result.get("order") if isinstance(result.get("order"), Mapping) else {}
    broker = result.get("broker_result") if isinstance(result.get("broker_result"), Mapping) else {}
    contract = str(order.get("symbol") or candidate.get("contract_symbol") or "").strip().upper()
    underlying = str(candidate.get("symbol") or candidate.get("underlying") or _occ_root(contract)).strip().upper()
    entry = JournalEntry(
        stage=stage,
        environment=PlatformEnvironment.PAPER,
        system=system,
        snapshot_id=snapshot_id,
        symbol=underlying or _occ_root(contract) or "UNKNOWN",
        contract_symbol=contract or None,
        decision=DeskDecision.TRADE_READY_RESEARCH,
        direction=_text(candidate.get("direction")),
        option_type=_text(candidate.get("option_type")),
        composite_score=_bounded100(candidate.get("composite_score")),
        ranking_score=_bounded100(candidate.get("ranking_score")),
        option_quality_score=_bounded100(candidate.get("option_quality_score")),
        regime_fit_score=_bounded100(candidate.get("regime_fit_score")),
        expected_return_pct=_number(candidate.get("expected_return_pct")),
        lower_confidence_bound_pct=_number(candidate.get("lower_confidence_bound_pct")),
        entry_ask=_nonnegative((result.get("quote") or {}).get("ask") if isinstance(result.get("quote"), Mapping) else candidate.get("entry_ask")),
        max_loss_usd_per_contract=_nonnegative(candidate.get("max_loss_usd_per_contract")),
        quantity=max(0, int(quantity)),
        hard_reasons=[str(item) for item in result.get("reasons", []) if str(item).strip()],
        option_feed=_text(result.get("option_feed") or (result.get("quote") or {}).get("feed") if isinstance(result.get("quote"), Mapping) else None),
        execution_grade_feed=(result.get("option_feed") == "opra"),
        broker_order_id=_text(broker.get("order_id")),
        metadata={
            "paper_decision": result.get("decision"),
            "submitted": bool(result.get("submitted")),
            "clock": result.get("clock"),
            "quote": result.get("quote"),
            "max_premium_risk_usd": result.get("max_premium_risk_usd"),
            "target_premium": result.get("target_premium"),
        },
    )
    store.append_journal_entry(entry)
    store.append_event(
        PlatformEvent(
            event_type="paper_order_lifecycle",
            environment=PlatformEnvironment.PAPER,
            system=system,
            payload={
                "journal_id": entry.journal_id,
                "stage": stage.value,
                "contract_symbol": contract or None,
                "broker_order_id": entry.broker_order_id,
                "decision": result.get("decision"),
            },
        )
    )


def _get_json(
    url: str,
    config: alpaca_sdk.AlpacaConfig,
    *,
    session: requests.Session | None,
) -> Mapping[str, Any]:
    if tap_forward.tap_enabled():
        payload = alpaca_sdk._read_via_tap(url)  # noqa: SLF001 - shared credential-isolated GET path
        if not isinstance(payload, Mapping):
            raise RuntimeError("Alpaca/TAP returned a non-object response")
        return payload
    if not config.api_key or not config.secret_key:
        raise alpaca_sdk.AlpacaConfigError("Alpaca read requires configured api_key and secret_key")
    client = session or requests.Session()
    response = client.get(
        url,
        headers={
            "APCA-API-KEY-ID": config.api_key,
            "APCA-API-SECRET-KEY": config.secret_key,
        },
        timeout=config.timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, Mapping):
        raise RuntimeError("Alpaca returned a non-object response")
    return payload


def _stage_from_broker_status(status: str) -> JournalStage | None:
    if status == "filled":
        return JournalStage.FILLED
    if status == "partially_filled":
        return JournalStage.PARTIAL_FILL
    if status in {"canceled", "cancelled"}:
        return JournalStage.CANCELLED
    if status == "expired":
        return JournalStage.EXPIRED
    if status in {"rejected", "suspended"}:
        return JournalStage.ERROR
    if status in {"new", "accepted", "pending_new", "accepted_for_bidding", "pending_replace"}:
        return JournalStage.SUBMITTED
    return None


def _occ_root(contract: str) -> str:
    text = str(contract or "").strip().upper()
    if len(text) < 15:
        return ""
    split = len(text) - 15
    root = text[:split]
    return root if 1 <= len(root) <= 6 and root.isalnum() else ""


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


def _nonnegative_int(value: object) -> int | None:
    number = _nonnegative(value)
    return None if number is None else int(number)


def _bounded100(value: object) -> float | None:
    number = _number(value)
    return None if number is None else min(100.0, max(0.0, number))


def _text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


__all__ = [
    "PaperLifecycleConfig",
    "append_reconciled_order_status",
    "fetch_alpaca_market_clock",
    "fetch_alpaca_order",
    "fetch_current_option_quote",
    "run_paper_option_lifecycle",
    "sync_paper_order_lifecycle",
]
