#!/usr/bin/env python3
"""Checkpoint mature candidate outcomes and write an attribution report.

This command is retrospective research only. It reads later option quotes to
label older journal decisions and never calls a broker or changes decision rules.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent"
if str(AGENT) not in sys.path:
    sys.path.insert(0, str(AGENT))

from src.options_market import OptionsResearchStore  # noqa: E402
from src.trading_platform import (  # noqa: E402
    AttributionConfig,
    TradingPlatformStore,
    build_attribution_report,
    checkpoint_candidate_outcomes,
)

UTC = timezone.utc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Checkpoint mature candidate outcomes")
    parser.add_argument("--platform-store", required=True, type=Path)
    parser.add_argument("--research-store", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--as-of", help="Evaluation timestamp; defaults to current UTC")
    parser.add_argument("--horizon-days", type=int, default=30)
    parser.add_argument("--target-profit-pct", type=float, default=300.0)
    parser.add_argument("--full-loss-threshold-pct", type=float, default=95.0)
    parser.add_argument("--minimum-quote-observations", type=int, default=2)
    parser.add_argument("--max-journal-scan", type=int, default=2000)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    as_of = _timestamp(args.as_of) if args.as_of else datetime.now(UTC)
    config = AttributionConfig(
        evaluation_horizon_days=args.horizon_days,
        target_profit_pct=args.target_profit_pct,
        full_loss_threshold_pct=args.full_loss_threshold_pct,
        minimum_quote_observations=args.minimum_quote_observations,
        max_journal_scan=args.max_journal_scan,
    )
    config.validate()

    with TradingPlatformStore(args.platform_store) as platform_store, OptionsResearchStore(args.research_store) as research_store:
        checkpoint = checkpoint_candidate_outcomes(
            platform_store=platform_store,
            research_store=research_store,
            as_of=as_of,
            config=config,
        )
        report = build_attribution_report(
            platform_store.recent_journal(limit=args.max_journal_scan)
        )

    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "as_of": as_of.isoformat(),
        "checkpoint": checkpoint,
        "attribution": report,
        "broker_mutation": False,
    }
    _atomic_json(args.output, payload)
    print(
        json.dumps(
            {
                "status": checkpoint.get("status"),
                "labeled": checkpoint.get("labeled"),
                "immature": checkpoint.get("immature"),
                "attribution_samples": report.get("samples"),
                "missed_opportunities": report.get("missed_opportunities"),
                "avoided_losses": report.get("avoided_losses"),
                "broker_mutation": False,
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 0


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("--as-of must include a timezone")
    return parsed.astimezone(UTC)


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
