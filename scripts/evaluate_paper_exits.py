#!/usr/bin/env python3
"""Evaluate open long-option paper positions for exit proposals.

This CLI is proposal-only. It reads position/quote JSON, carries trailing
high-watermarks across restarts, optionally journals EXIT_PROPOSED states, and
writes an atomic report. It never calls a broker mutation endpoint.
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

from src.trading_platform import (  # noqa: E402
    ExitPolicyConfig,
    TradingPlatformStore,
    scan_long_option_exits,
)

UTC = timezone.utc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate proposal-only paper option exits")
    parser.add_argument("--positions-json", required=True, type=Path)
    parser.add_argument("--quotes-json", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--state",
        type=Path,
        default=Path("data/paper-exit-watermarks.json"),
        help="Restart-safe option high-watermark state",
    )
    parser.add_argument("--platform-store", type=Path, help="Optional Trading Platform DuckDB for journaling")
    parser.add_argument("--snapshot-id")
    parser.add_argument("--max-quote-age-seconds", type=float, default=90.0)
    parser.add_argument("--hard-stop-loss-pct", type=float, default=50.0)
    parser.add_argument("--profit-take-pct", type=float, default=100.0)
    parser.add_argument("--trailing-activation-gain-pct", type=float, default=75.0)
    parser.add_argument("--trailing-drawdown-from-peak-pct", type=float, default=25.0)
    parser.add_argument("--max-dte-to-hold", type=int, default=3)
    parser.add_argument("--max-holding-days", type=int, default=20)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    now = datetime.now(UTC)
    positions = _positions(args.positions_json)
    quotes = _quotes(args.quotes_json)
    state = _load_state(args.state)
    _apply_watermarks(positions, state)

    config = ExitPolicyConfig(
        max_quote_age_seconds=args.max_quote_age_seconds,
        hard_stop_loss_pct=args.hard_stop_loss_pct,
        profit_take_pct=args.profit_take_pct,
        trailing_activation_gain_pct=args.trailing_activation_gain_pct,
        trailing_drawdown_from_peak_pct=args.trailing_drawdown_from_peak_pct,
        max_dte_to_hold=args.max_dte_to_hold,
        max_holding_days=args.max_holding_days,
    )
    config.validate()

    if args.platform_store:
        with TradingPlatformStore(args.platform_store) as store:
            report = scan_long_option_exits(
                positions,
                quotes,
                now=now,
                config=config,
                store=store,
                snapshot_id=args.snapshot_id,
            )
    else:
        report = scan_long_option_exits(
            positions,
            quotes,
            now=now,
            config=config,
            snapshot_id=args.snapshot_id,
        )

    _update_watermarks(state, report.get("results", []), now=now)
    _atomic_json(args.state, state)
    _atomic_json(args.output, report)
    print(
        json.dumps(
            {
                "status": report.get("status"),
                "positions_evaluated": report.get("positions_evaluated"),
                "exit_proposals": report.get("exit_proposals"),
                "blocked_no_action": report.get("blocked_no_action"),
                "broker_mutation": False,
                "output": str(args.output),
                "state": str(args.state),
            },
            ensure_ascii=False,
        )
    )
    return 0


def _positions(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, Mapping):
        payload = payload.get("positions")
    if not isinstance(payload, list):
        raise ValueError("positions JSON must be a list or contain a positions list")
    return [dict(row) for row in payload if isinstance(row, Mapping)]


def _quotes(path: Path) -> dict[str, dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, Mapping) and isinstance(payload.get("quotes"), Mapping):
        payload = payload.get("quotes")
    if not isinstance(payload, Mapping):
        raise ValueError("quotes JSON must map contract symbol to quote object")
    return {
        str(symbol).strip().upper().replace(" ", ""): dict(row)
        for symbol, row in payload.items()
        if isinstance(row, Mapping)
    }


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, "positions": {}}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("exit watermark state must be a JSON object")
    positions = payload.get("positions")
    return {
        "schema_version": 1,
        "positions": dict(positions) if isinstance(positions, Mapping) else {},
        "updated_at": payload.get("updated_at"),
    }


def _apply_watermarks(positions: list[dict[str, Any]], state: Mapping[str, Any]) -> None:
    rows = state.get("positions") if isinstance(state.get("positions"), Mapping) else {}
    for position in positions:
        contract = str(position.get("contract_symbol") or position.get("symbol") or "").strip().upper().replace(" ", "")
        saved = rows.get(contract) if isinstance(rows, Mapping) else None
        if not isinstance(saved, Mapping):
            continue
        watermark = saved.get("high_watermark_bid")
        if watermark is not None and position.get("high_watermark_bid") is None:
            position["high_watermark_bid"] = watermark


def _update_watermarks(state: dict[str, Any], results: object, *, now: datetime) -> None:
    rows = state.setdefault("positions", {})
    if not isinstance(rows, dict):
        rows = {}
        state["positions"] = rows
    if isinstance(results, list):
        for result in results:
            if not isinstance(result, Mapping) or result.get("decision") == "NO_ACTION":
                continue
            contract = str(result.get("contract_symbol") or "").strip().upper()
            watermark = result.get("high_watermark_bid")
            if contract and watermark is not None:
                rows[contract] = {
                    "high_watermark_bid": watermark,
                    "last_bid": result.get("current_bid"),
                    "updated_at": now.isoformat(),
                }
    state["updated_at"] = now.isoformat()


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
