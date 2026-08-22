#!/usr/bin/env python3
"""Prepare or submit one gated long-option order to Alpaca paper.

Examples:

  # Dry-run only (default)
  python scripts/prepare_option_paper_order.py candidate.json

  # Intentionally mutate the paper account after all reports are present and
  # the caller has independently confirmed the regular session is open.
  python scripts/prepare_option_paper_order.py candidate.json \
      --submit-paper --market-open

The input JSON must contain ``candidate``, ``ev_report``,
``walk_forward_report`` and ``risk_report`` objects.  Live Alpaca profiles are
rejected inside the execution module regardless of CLI flags.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent"
if str(AGENT) not in sys.path:
    sys.path.insert(0, str(AGENT))

from src.options_market.paper_execution import (  # noqa: E402
    PaperExecutionConfig,
    submit_paper_option_order,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare/submit a gated Alpaca paper option order")
    parser.add_argument("input", type=Path, help="JSON file containing candidate + gate reports")
    parser.add_argument("--quantity", type=int, default=1, help="Whole option contracts; default 1")
    parser.add_argument("--bid", type=float, default=None, help="Latest option bid")
    parser.add_argument("--ask", type=float, default=None, help="Latest option ask")
    parser.add_argument("--limit-price", type=float, default=None, help="Maximum premium per share")
    parser.add_argument(
        "--submit-paper",
        action="store_true",
        help="Actually submit to Alpaca PAPER; without this flag the command is dry-run only",
    )
    parser.add_argument(
        "--market-open",
        action="store_true",
        help="Explicit caller assertion that the regular options session is open",
    )
    parser.add_argument("--max-premium-risk", type=float, default=500.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    candidate = payload.get("candidate") or {}
    ev_report = payload.get("ev_report") or {}
    walk_report = payload.get("walk_forward_report") or {}
    risk_report = payload.get("risk_report") or {}

    cfg = PaperExecutionConfig(
        dry_run=not args.submit_paper,
        max_contracts=max(1, args.quantity),
        max_premium_risk_usd=args.max_premium_risk,
    )
    result = submit_paper_option_order(
        candidate,
        quantity=args.quantity,
        bid=args.bid,
        ask=args.ask,
        limit_price=args.limit_price,
        market_is_open=args.market_open,
        ev_report=ev_report,
        walk_forward_report=walk_report,
        risk_report=risk_report,
        config=cfg,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0 if result.get("decision") in {"PAPER_ORDER_DRY_RUN", "PAPER_ORDER_SUBMITTED"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
