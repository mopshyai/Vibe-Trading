#!/usr/bin/env python3
"""Generate an authoritative XNYS session file for the continuous analyzer."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent"
if str(AGENT) not in sys.path:
    sys.path.insert(0, str(AGENT))

from src.trading_platform import DataPlaneManifest  # noqa: E402
from src.trading_platform.market_calendar import xnys_session_calendar  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate XNYS market sessions")
    parser.add_argument("--start", required=True, help="First calendar date, YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="Last calendar date, YYYY-MM-DD")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--manifest", type=Path, help="Optional data-plane freshness manifest")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = xnys_session_calendar(args.start, args.end)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    observed = datetime.now(timezone.utc)
    if args.manifest:
        DataPlaneManifest(args.manifest).mark_success(
            "market_calendar",
            observed_at=observed,
            source="exchange_calendars:XNYS",
            detail=f"calendar {args.start} through {args.end}",
            metadata={"output": str(args.output), "dates": len(payload)},
        )
    print(f"wrote {len(payload)} calendar dates to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
