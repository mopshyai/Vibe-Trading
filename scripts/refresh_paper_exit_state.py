#!/usr/bin/env python3
"""Refresh open PAPER option positions and exact quotes for exit evaluation.

The command performs broker reads only. It converts Alpaca option positions into
the exit-manager contract, retrieves each exact option snapshot, recovers fill
context from the append-only journal, and carries only true account kill signals
into ``account_risk_exit_required``. It never submits, replaces, cancels, or
closes an order.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent"
if str(AGENT) not in sys.path:
    sys.path.insert(0, str(AGENT))

from src.trading_platform.paper_broker_read import fetch_paper_positions  # noqa: E402
from src.trading_platform.paper_lifecycle import fetch_current_option_quote  # noqa: E402
from src.trading_platform.runtime_config import load_alpaca_runtime_config  # noqa: E402
from src.trading_platform.store import TradingPlatformStore  # noqa: E402

UTC = timezone.utc
_KILL_REASONS = {
    "daily_loss_kill_threshold",
    "weekly_loss_kill_threshold",
    "account_drawdown_kill_threshold",
    "broker_account_trading_blocked",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh read-only PAPER option exit state")
    parser.add_argument("--positions-output", required=True, type=Path)
    parser.add_argument("--quotes-output", required=True, type=Path)
    parser.add_argument("--store", required=True, type=Path, help="Trading Platform DuckDB journal")
    parser.add_argument("--risk-json", type=Path, help="Detailed account-risk report")
    parser.add_argument("--feed", choices=["opra", "indicative"], default="opra")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    now = datetime.now(UTC)
    config = load_alpaca_runtime_config()
    broker_positions = fetch_paper_positions(config)
    risk = _json(args.risk_json) if args.risk_json else {}
    risk_exit_required, risk_exit_reasons = _risk_exit_signal(risk)

    positions: list[dict[str, Any]] = []
    quotes: dict[str, dict[str, Any]] = {}
    statuses: list[dict[str, Any]] = []

    with TradingPlatformStore(args.store) as store:
        for raw in broker_positions:
            if str(raw.get("asset_class") or "").strip().lower() != "option":
                continue
            contract = str(raw.get("symbol") or "").strip().upper().replace(" ", "")
            if not contract:
                continue
            fill = _fill_context(store.recent_journal(limit=250, contract_symbol=contract))
            position = {
                "contract_symbol": contract,
                "asset_class": "option",
                "quantity": raw.get("qty"),
                "side": raw.get("side"),
                "avg_entry_price": raw.get("avg_entry_price"),
                "market_value": raw.get("market_value"),
                "cost_basis": raw.get("cost_basis"),
                "current_price": raw.get("current_price"),
                "filled_at": fill.get("filled_at"),
                "broker_order_id": fill.get("broker_order_id"),
                "account_risk_exit_required": risk_exit_required,
                "account_risk_exit_reasons": risk_exit_reasons,
            }
            positions.append(position)
            try:
                quote = fetch_current_option_quote(
                    contract,
                    config,
                    feed=args.feed,
                    now=now,
                )
            except Exception as exc:  # noqa: BLE001 - per-contract reads fail independently
                statuses.append(
                    {
                        "contract_symbol": contract,
                        "status": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            quotes[contract] = quote
            statuses.append(
                {
                    "contract_symbol": contract,
                    "status": "ok",
                    "feed": quote.get("feed"),
                    "quote_age_seconds": quote.get("age_seconds"),
                }
            )

    errors = [row for row in statuses if row.get("status") == "error"]
    status = "ok" if not errors else "partial"
    positions_payload = {
        "schema_version": 1,
        "observed_at": now.isoformat(),
        "profile": "paper",
        "positions": positions,
        "option_positions": len(positions),
        "account_risk_exit_required": risk_exit_required,
        "account_risk_exit_reasons": risk_exit_reasons,
        "broker_mutation": False,
    }
    quotes_payload = {
        "schema_version": 1,
        "observed_at": now.isoformat(),
        "feed": args.feed,
        "quotes": quotes,
        "statuses": statuses,
        "broker_mutation": False,
    }
    _atomic_json(args.positions_output, positions_payload)
    _atomic_json(args.quotes_output, quotes_payload)
    print(
        json.dumps(
            {
                "status": status,
                "option_positions": len(positions),
                "quotes_refreshed": len(quotes),
                "quote_errors": len(errors),
                "account_risk_exit_required": risk_exit_required,
                "broker_mutation": False,
            },
            ensure_ascii=False,
        )
    )
    return 0 if not errors else 2


def _risk_exit_signal(report: object) -> tuple[bool, list[str]]:
    if not isinstance(report, Mapping):
        return False, []
    reasons = [str(item) for item in report.get("blocking_reasons", []) if str(item).strip()]
    severe = sorted(set(reasons).intersection(_KILL_REASONS))
    return bool(severe), severe


def _fill_context(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    partial: Mapping[str, Any] | None = None
    for row in rows:
        stage = str(row.get("stage") or "").strip().lower()
        if stage == "filled":
            return _fill_from_row(row)
        if stage == "partial_fill" and partial is None:
            partial = row
    return _fill_from_row(partial) if partial is not None else {}


def _fill_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), Mapping) else {}
    broker = metadata.get("broker_order") if isinstance(metadata.get("broker_order"), Mapping) else {}
    filled_at = broker.get("filled_at") or row.get("occurred_at")
    return {
        "filled_at": str(filled_at).strip() if filled_at else None,
        "broker_order_id": str(row.get("broker_order_id") or "").strip() or None,
    }


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False, default=str) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


if __name__ == "__main__":
    raise SystemExit(main())
