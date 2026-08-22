#!/usr/bin/env python3
"""Prepare, submit, or reconcile supervised Alpaca paper option orders.

The default action is a dry-run proposal. Actual paper submission requires both
``--submit-paper`` and ``--confirm-paper-submit``. Live Alpaca profiles are
rejected by the lifecycle module.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent"
if str(AGENT) not in sys.path:
    sys.path.insert(0, str(AGENT))

from src.config.paths import get_runtime_root  # noqa: E402
from src.trading_platform import SystemIdentity, TradingPlatformStore  # noqa: E402
from src.trading_platform.paper_lifecycle import (  # noqa: E402
    PaperLifecycleConfig,
    run_paper_option_lifecycle,
    sync_paper_order_lifecycle,
)
from src.trading_platform.runtime_config import load_alpaca_runtime_config  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run supervised Alpaca paper option lifecycle")
    parser.add_argument("--input-json", type=Path, help="JSON with candidate, ev_report, walk_forward_report and risk_report")
    parser.add_argument("--store", type=Path, default=None)
    parser.add_argument("--sync-only", action="store_true", help="Reconcile previously journaled paper orders")
    parser.add_argument("--submit-paper", action="store_true", help="Request a paper broker submission")
    parser.add_argument(
        "--confirm-paper-submit",
        action="store_true",
        help="Second explicit confirmation required with --submit-paper",
    )
    parser.add_argument("--quantity", type=int, default=1)
    parser.add_argument("--limit-price", type=float)
    parser.add_argument("--option-feed", choices=["opra", "indicative"], default="opra")
    parser.add_argument("--max-quote-age-seconds", type=float, default=90.0)
    parser.add_argument("--max-premium-risk", type=float, default=500.0)
    parser.add_argument("--max-contracts", type=int, default=1)
    parser.add_argument("--snapshot-id")
    parser.add_argument("--commit-sha")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.submit_paper and not args.confirm_paper_submit:
        raise ValueError("--submit-paper requires --confirm-paper-submit")
    if not args.sync_only and args.input_json is None:
        raise ValueError("--input-json is required unless --sync-only is used")

    store_path = args.store or (get_runtime_root() / "trading-platform.duckdb")
    store_path.parent.mkdir(parents=True, exist_ok=True)
    system = SystemIdentity(commit_sha=str(args.commit_sha or "").strip() or None)
    broker_config = load_alpaca_runtime_config()

    with TradingPlatformStore(store_path) as store:
        if args.sync_only:
            result = sync_paper_order_lifecycle(
                store=store,
                system=system,
                alpaca_config=broker_config,
            )
        else:
            payload = _mapping(_json(args.input_json), "input JSON")
            candidate = _mapping(payload.get("candidate"), "candidate")
            ev_report = _mapping(payload.get("ev_report"), "ev_report")
            walk_forward = _mapping(payload.get("walk_forward_report"), "walk_forward_report")
            risk_report = _mapping(payload.get("risk_report"), "risk_report")
            result = run_paper_option_lifecycle(
                candidate,
                ev_report=ev_report,
                walk_forward_report=walk_forward,
                risk_report=risk_report,
                quantity=args.quantity,
                limit_price=args.limit_price,
                submit=args.submit_paper,
                confirm_submit=args.confirm_paper_submit,
                alpaca_config=broker_config,
                config=PaperLifecycleConfig(
                    option_feed=args.option_feed,
                    max_quote_age_seconds=args.max_quote_age_seconds,
                    max_contracts=args.max_contracts,
                    max_premium_risk_usd=args.max_premium_risk,
                ),
                store=store,
                system=system,
                snapshot_id=str(args.snapshot_id or "").strip() or None,
            )

    rendered = json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False, default=str) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")

    if result.get("status") == "error":
        return 2
    if result.get("decision") in {"PAPER_RUNTIME_REJECTED", "PAPER_ORDER_REJECTED", "PAPER_ORDER_SUBMISSION_FAILED"}:
        return 2
    return 0


def _json(path: Path | None) -> Any:
    if path is None:
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    return dict(value)


if __name__ == "__main__":
    raise SystemExit(main())
