"""Operational readiness preflight for the personal Trading Desk.

The preflight is read-only. It distinguishes three different states that should
never be conflated:

* platform_operational: software/store/calendar are usable;
* paper_runtime_ready: Alpaca paper account and OPRA access are reachable;
* paper_submit_ready_now: a specific candidate also has passing evidence, fresh
  execution data, an unblocked account and an open market.

Secrets are never returned. Missing credentials/entitlements are converted into
specific operator actions instead of dumping configuration values.
"""

from __future__ import annotations

from datetime import datetime, timezone
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
from typing import Any, Mapping

import requests

from src.trading import tap_forward
from src.trading.connectors.alpaca import sdk as alpaca_sdk

from .paper_lifecycle import fetch_alpaca_market_clock, fetch_current_option_quote
from .store import TradingPlatformStore

UTC = timezone.utc


def run_platform_preflight(
    *,
    store_path: str | Path,
    execution_input: Mapping[str, Any] | None = None,
    alpaca_config: alpaca_sdk.AlpacaConfig | None = None,
    option_contract: str | None = None,
    session: requests.Session | None = None,
    now: datetime | None = None,
    max_quote_age_seconds: float = 90.0,
) -> dict[str, Any]:
    """Run read-only software, data, broker and execution-readiness checks."""
    observed_at = _aware_now(now)
    checks: dict[str, dict[str, Any]] = {}
    operator_actions: list[str] = []
    current_conditions: list[str] = []

    checks["calendar_runtime"] = _calendar_check()

    snapshot = None
    store_error = None
    try:
        with TradingPlatformStore(store_path) as store:
            snapshot = store.latest_snapshot()
            counts = store.counts()
        checks["trading_store"] = {
            "ok": True,
            "path": str(store_path),
            "snapshot_present": snapshot is not None,
            "counts": counts,
        }
    except Exception as exc:  # noqa: BLE001 - preflight reports, never hides, readiness failures
        store_error = f"{type(exc).__name__}: {exc}"
        checks["trading_store"] = {"ok": False, "error": store_error}

    if snapshot is not None:
        checks["desk_data_quality"] = {
            "ok": bool(snapshot.data_quality.healthy),
            "decision": snapshot.decision.value,
            "blocking_reasons": list(snapshot.data_quality.blocking_reasons),
            "created_at": snapshot.created_at.isoformat(),
        }
        checks["desk_account_risk"] = {
            "ok": not bool(snapshot.risk.trading_blocked),
            "trading_blocked": bool(snapshot.risk.trading_blocked),
            "blocking_reasons": list(snapshot.risk.blocking_reasons),
            "account_equity_usd": snapshot.risk.account_equity_usd,
            "open_premium_risk_pct": snapshot.risk.open_premium_risk_pct,
        }
    else:
        checks["desk_data_quality"] = {"ok": False, "reason": "no_trading_desk_snapshot"}
        checks["desk_account_risk"] = {"ok": False, "reason": "no_trading_desk_snapshot"}

    payload = dict(execution_input or {})
    candidate = payload.get("candidate") if isinstance(payload.get("candidate"), Mapping) else {}
    if not candidate and snapshot is not None:
        ready_cards = [card for card in snapshot.opportunities if card.decision.value == "TRADE_READY_RESEARCH"]
        source = ready_cards[0] if ready_cards else (snapshot.opportunities[0] if snapshot.opportunities else None)
        if source is not None:
            candidate = source.model_dump(mode="json")
    contract = str(option_contract or candidate.get("contract_symbol") or "").strip().upper().replace(" ", "")

    evidence = _evidence_check(payload)
    checks["execution_evidence"] = evidence

    broker_cfg = alpaca_config or alpaca_sdk.load_config()
    local_credentials_present = bool(broker_cfg.api_key and broker_cfg.secret_key)
    tap_enabled = tap_forward.tap_enabled()
    paper_profile = broker_cfg.profile == "paper" and broker_cfg.is_paper and broker_cfg.host == alpaca_sdk.PAPER_HOST
    checks["alpaca_configuration"] = {
        "ok": paper_profile and (tap_enabled or local_credentials_present),
        "profile": broker_cfg.profile,
        "is_paper": broker_cfg.is_paper,
        "host": broker_cfg.host,
        "tap_enabled": tap_enabled,
        "credentials_configured": tap_enabled or local_credentials_present,
    }
    if not paper_profile:
        operator_actions.append("configure_alpaca_profile_as_paper")
    if not (tap_enabled or local_credentials_present):
        operator_actions.append("configure_alpaca_paper_credentials_in_runtime_or_TAP")

    account: dict[str, Any] | None = None
    clock: dict[str, Any] | None = None
    broker_reads_allowed = paper_profile and (tap_enabled or local_credentials_present)
    if broker_reads_allowed:
        try:
            account = _fetch_paper_account(broker_cfg, session=session)
            blocked = bool(account.get("trading_blocked"))
            checks["alpaca_paper_account"] = {
                "ok": not blocked,
                "status": account.get("status"),
                "trading_blocked": blocked,
                "equity": account.get("equity"),
                "buying_power": account.get("buying_power"),
            }
            if blocked:
                operator_actions.append("resolve_alpaca_paper_account_trading_block")
        except Exception as exc:  # noqa: BLE001
            checks["alpaca_paper_account"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            operator_actions.append("verify_alpaca_paper_credentials_and_account_access")

        try:
            clock = fetch_alpaca_market_clock(broker_cfg, session=session)
            checks["market_clock"] = {"ok": True, **clock}
            if not bool(clock.get("is_open")):
                current_conditions.append("us_options_market_closed")
        except Exception as exc:  # noqa: BLE001
            checks["market_clock"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    else:
        checks["alpaca_paper_account"] = {"ok": False, "reason": "broker_configuration_not_ready"}
        checks["market_clock"] = {"ok": False, "reason": "broker_configuration_not_ready"}

    opra_quote = None
    if contract and broker_reads_allowed:
        try:
            opra_quote = fetch_current_option_quote(
                contract,
                broker_cfg,
                feed="opra",
                session=session,
                now=observed_at,
            )
            age = opra_quote.get("age_seconds")
            fresh = age is not None and float(age) <= max_quote_age_seconds
            checks["opra_option_data"] = {
                "ok": True,
                "contract_symbol": contract,
                "access": True,
                "fresh": fresh,
                "quote_age_seconds": age,
                "bid": opra_quote.get("bid"),
                "ask": opra_quote.get("ask"),
            }
            if not fresh:
                current_conditions.append("opra_quote_not_fresh_for_submission")
        except Exception as exc:  # noqa: BLE001
            checks["opra_option_data"] = {
                "ok": False,
                "contract_symbol": contract,
                "access": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
            operator_actions.append("verify_Alpaca_OPRA_option_data_entitlement")
    elif not contract:
        checks["opra_option_data"] = {"ok": False, "reason": "no_option_contract_available_to_probe"}
    else:
        checks["opra_option_data"] = {"ok": False, "reason": "broker_configuration_not_ready"}

    databento_present = bool(os.environ.get("DATABENTO_API_KEY"))
    checks["historical_replay_provider"] = {
        "ok": databento_present,
        "provider": "databento",
        "credential_present": databento_present,
    }
    if not databento_present:
        operator_actions.append("set_DATABENTO_API_KEY_in_runtime_for_historical_replay")

    software_ok = bool(checks["calendar_runtime"].get("ok")) and bool(checks["trading_store"].get("ok"))
    paper_account_ok = bool(checks["alpaca_configuration"].get("ok")) and bool(checks["alpaca_paper_account"].get("ok"))
    opra_access = bool(checks["opra_option_data"].get("access"))
    paper_runtime_ready = software_ok and paper_account_ok and opra_access

    market_open = bool(clock and clock.get("is_open"))
    fresh_opra = bool(checks["opra_option_data"].get("fresh"))
    desk_data_ok = bool(checks["desk_data_quality"].get("ok"))
    desk_risk_ok = bool(checks["desk_account_risk"].get("ok"))
    evidence_ok = bool(evidence.get("ok"))
    paper_submit_ready_now = all(
        [paper_runtime_ready, market_open, fresh_opra, desk_data_ok, desk_risk_ok, evidence_ok, bool(contract)]
    )

    return {
        "schema_version": 1,
        "observed_at": observed_at.isoformat(),
        "platform_operational": software_ok,
        "research_data_ready": desk_data_ok,
        "historical_replay_ready": databento_present,
        "paper_runtime_ready": paper_runtime_ready,
        "paper_submit_ready_now": paper_submit_ready_now,
        "candidate_contract": contract or None,
        "checks": checks,
        "operator_actions": _dedupe(operator_actions),
        "current_conditions": _dedupe(current_conditions),
        "notes": [
            "Preflight is read-only and never places, cancels or replaces an order.",
            "paper_submit_ready_now is intentionally false unless a specific candidate's EV, walk-forward and risk reports are supplied and passing.",
        ],
    }


def _calendar_check() -> dict[str, Any]:
    if importlib.util.find_spec("exchange_calendars") is None:
        return {"ok": False, "reason": "exchange_calendars_not_installed"}
    try:
        import exchange_calendars as xcals

        calendar = xcals.get_calendar("XNYS")
        del calendar
        version = importlib.metadata.version("exchange-calendars")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "package": "exchange-calendars", "version": version, "calendar": "XNYS"}


def _evidence_check(payload: Mapping[str, Any]) -> dict[str, Any]:
    ev = payload.get("ev_report") if isinstance(payload.get("ev_report"), Mapping) else {}
    walk = payload.get("walk_forward_report") if isinstance(payload.get("walk_forward_report"), Mapping) else {}
    risk = payload.get("risk_report") if isinstance(payload.get("risk_report"), Mapping) else {}
    supplied = bool(ev or walk or risk)
    positive_ev = bool(ev.get("positive_ev"))
    walk_pass = walk.get("decision") == "WALK_FORWARD_PASS"
    risk_pass = bool(risk.get("approved"))
    return {
        "ok": supplied and positive_ev and walk_pass and risk_pass,
        "supplied": supplied,
        "positive_ev": positive_ev,
        "walk_forward_pass": walk_pass,
        "candidate_risk_approved": risk_pass,
    }


def _fetch_paper_account(
    config: alpaca_sdk.AlpacaConfig,
    *,
    session: requests.Session | None,
) -> dict[str, Any]:
    if not config.is_paper or config.profile != "paper" or config.host != alpaca_sdk.PAPER_HOST:
        raise RuntimeError("Alpaca paper profile required")
    payload = _alpaca_get(f"{alpaca_sdk.PAPER_HOST}/v2/account", config, session=session)
    return {
        "status": str(payload.get("status") or ""),
        "currency": payload.get("currency"),
        "cash": payload.get("cash"),
        "equity": payload.get("equity"),
        "buying_power": payload.get("buying_power"),
        "portfolio_value": payload.get("portfolio_value"),
        "pattern_day_trader": payload.get("pattern_day_trader"),
        "trading_blocked": payload.get("trading_blocked"),
    }


def _alpaca_get(
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
        raise alpaca_sdk.AlpacaConfigError("Alpaca paper credentials are not configured")
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


def _aware_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(UTC)


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


__all__ = ["run_platform_preflight"]
