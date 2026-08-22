#!/usr/bin/env python3
"""Assess current personal-account risk without mutating a broker.

Inputs can come from a JSON fixture/export or the existing read-only Alpaca
account/positions connector. Optional enrichment JSON can add Greeks, underlying
prices, sectors or themes keyed by contract/symbol.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent"
if str(AGENT) not in sys.path:
    sys.path.insert(0, str(AGENT))

from src.trading.connectors.alpaca import sdk as alpaca_sdk  # noqa: E402
from src.trading_platform.portfolio_risk import (  # noqa: E402
    AccountRiskConfig,
    assess_account_portfolio_risk,
    risk_summary_from_report,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Assess personal account portfolio risk")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--positions-json", type=Path, help="JSON list or {positions:[...], equity:...}")
    source.add_argument("--alpaca-account", action="store_true", help="Use configured Alpaca account/positions reads")
    parser.add_argument("--account-equity", type=float, help="Override/provide account equity")
    parser.add_argument("--enrichment-json", type=Path, help="Contract/symbol -> Greeks/sector/spot mapping")
    parser.add_argument("--returns-json", type=Path, help="Underlying -> return-series mapping")
    parser.add_argument("--equity-curve-json", type=Path, help="JSON array of account equity history")
    parser.add_argument("--daily-realized-pnl", type=float)
    parser.add_argument("--weekly-realized-pnl", type=float)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--summary-output", type=Path, help="Write Trading Desk RiskSummary JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.alpaca_account:
        account = alpaca_sdk.get_account_snapshot()
        positions_result = alpaca_sdk.get_positions()
        positions = [dict(row) for row in positions_result.get("positions", []) if isinstance(row, Mapping)]
        account_equity = _number(args.account_equity) or _number(account.get("account", {}).get("equity"))
    else:
        payload = _json(args.positions_json)
        if isinstance(payload, list):
            positions = [dict(row) for row in payload if isinstance(row, Mapping)]
            embedded_equity = None
        elif isinstance(payload, Mapping):
            rows = payload.get("positions")
            if not isinstance(rows, list):
                raise ValueError("positions JSON object must contain a positions list")
            positions = [dict(row) for row in rows if isinstance(row, Mapping)]
            embedded_equity = _number(payload.get("equity") or payload.get("account_equity_usd"))
        else:
            raise ValueError("positions JSON must be a list or object")
        account_equity = _number(args.account_equity) or embedded_equity

    if account_equity is None or account_equity <= 0:
        raise ValueError("account equity is required and must be positive")

    enrichment = _mapping_file(args.enrichment_json)
    enriched_positions = [_enrich(row, enrichment) for row in positions]
    returns = _returns(args.returns_json)
    equity_curve = _equity_curve(args.equity_curve_json)

    report = assess_account_portfolio_risk(
        enriched_positions,
        account_equity_usd=account_equity,
        daily_realized_pnl_usd=args.daily_realized_pnl,
        weekly_realized_pnl_usd=args.weekly_realized_pnl,
        equity_curve=equity_curve,
        returns_by_underlying=returns,
        config=AccountRiskConfig(),
    )
    summary = risk_summary_from_report(report)
    rendered = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False, default=str) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    if args.summary_output:
        args.summary_output.parent.mkdir(parents=True, exist_ok=True)
        args.summary_output.write_text(
            json.dumps(summary.model_dump(mode="json"), indent=2, ensure_ascii=False, allow_nan=False) + "\n",
            encoding="utf-8",
        )
    return 0 if report.get("approved") else 2


def _enrich(position: Mapping[str, Any], enrichment: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(position)
    keys = [
        str(row.get("contract_symbol") or "").strip().upper(),
        str(row.get("symbol") or "").strip().upper(),
        str(row.get("underlying") or "").strip().upper(),
    ]
    for key in keys:
        extra = enrichment.get(key)
        if isinstance(extra, Mapping):
            row.update(dict(extra))
            break
    return row


def _returns(path: Path | None) -> dict[str, pd.Series]:
    payload = _mapping_file(path)
    output: dict[str, pd.Series] = {}
    for symbol, values in payload.items():
        if isinstance(values, list):
            output[str(symbol).upper()] = pd.Series(values, dtype=float)
    return output


def _equity_curve(path: Path | None) -> pd.Series | None:
    if path is None:
        return None
    payload = _json(path)
    if not isinstance(payload, list):
        raise ValueError("equity-curve JSON must be a list")
    return pd.Series(payload, dtype=float)


def _mapping_file(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    payload = _json(path)
    if not isinstance(payload, Mapping):
        raise ValueError(f"{path} must contain a JSON object")
    return dict(payload)


def _json(path: Path | None) -> Any:
    if path is None:
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number == number else None


if __name__ == "__main__":
    raise SystemExit(main())
