"""Read-only Alpaca paper account/position access for hosted platform workers."""

from __future__ import annotations

from typing import Any, Mapping

import requests

from src.trading import tap_forward
from src.trading.connectors.alpaca import sdk as alpaca_sdk


def fetch_paper_account(
    config: alpaca_sdk.AlpacaConfig,
    *,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    _require_paper(config)
    payload = _get(f"{alpaca_sdk.PAPER_HOST}/v2/account", config, session=session)
    if not isinstance(payload, Mapping):
        raise RuntimeError("Alpaca paper account response must be an object")
    return {
        "status": payload.get("status"),
        "currency": payload.get("currency"),
        "cash": payload.get("cash"),
        "equity": payload.get("equity"),
        "buying_power": payload.get("buying_power"),
        "portfolio_value": payload.get("portfolio_value"),
        "pattern_day_trader": payload.get("pattern_day_trader"),
        "trading_blocked": bool(payload.get("trading_blocked")),
        "account_blocked": bool(payload.get("account_blocked")),
        "trade_suspended_by_user": bool(payload.get("trade_suspended_by_user")),
    }


def fetch_paper_positions(
    config: alpaca_sdk.AlpacaConfig,
    *,
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    _require_paper(config)
    payload = _get(f"{alpaca_sdk.PAPER_HOST}/v2/positions", config, session=session)
    if not isinstance(payload, list):
        raise RuntimeError("Alpaca paper positions response must be a list")
    rows: list[dict[str, Any]] = []
    for raw in payload:
        if not isinstance(raw, Mapping):
            continue
        raw_asset_class = str(raw.get("asset_class") or "").strip().lower()
        asset_class = (
            "option"
            if raw_asset_class in {"option", "us_option"}
            else "equity"
            if raw_asset_class in {"equity", "us_equity"}
            else raw_asset_class
        )
        rows.append(
            {
                "symbol": raw.get("symbol"),
                "asset_id": raw.get("asset_id"),
                "asset_class": asset_class,
                "broker_asset_class": raw_asset_class or None,
                "exchange": raw.get("exchange"),
                "qty": raw.get("qty"),
                "side": raw.get("side"),
                "avg_entry_price": raw.get("avg_entry_price"),
                "market_value": raw.get("market_value"),
                "cost_basis": raw.get("cost_basis"),
                "unrealized_pl": raw.get("unrealized_pl"),
                "unrealized_plpc": raw.get("unrealized_plpc"),
                "current_price": raw.get("current_price"),
                "lastday_price": raw.get("lastday_price"),
                "change_today": raw.get("change_today"),
            }
        )
    return rows


def fetch_paper_account_positions(
    config: alpaca_sdk.AlpacaConfig,
    *,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Read account and positions from the same explicit paper profile."""
    return {
        "account": fetch_paper_account(config, session=session),
        "positions": fetch_paper_positions(config, session=session),
        "profile": "paper",
        "is_paper": True,
    }


def _require_paper(config: alpaca_sdk.AlpacaConfig) -> None:
    if not config.is_paper or config.profile != "paper" or config.host != alpaca_sdk.PAPER_HOST:
        raise RuntimeError("Alpaca paper profile required")


def _get(
    url: str,
    config: alpaca_sdk.AlpacaConfig,
    *,
    session: requests.Session | None,
) -> Any:
    if tap_forward.tap_enabled():
        return alpaca_sdk._read_via_tap(url)  # noqa: SLF001 - shared credential-isolated GET path
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
    return response.json()


__all__ = [
    "fetch_paper_account",
    "fetch_paper_account_positions",
    "fetch_paper_positions",
]
