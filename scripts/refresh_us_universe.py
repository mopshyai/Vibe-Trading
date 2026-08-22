#!/usr/bin/env python3
"""Refresh the current U.S. listed-security universe from Nasdaq Trader.

The output contains both the compact ``symbols`` list consumed by the continuous
scanner and listing metadata for lineage/debugging. The write is atomic so a
reader never observes a partially written universe.
"""

from __future__ import annotations

from dataclasses import asdict
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

from src.options_market.universe import fetch_us_listed_universe  # noqa: E402
from src.trading_platform import DataPlaneManifest  # noqa: E402


def parse_args():  # type: ignore[no-untyped-def]
    import argparse

    parser = argparse.ArgumentParser(description="Refresh U.S. listed-security universe")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--manifest", type=Path, help="Optional data-plane freshness manifest")
    parser.add_argument("--exclude-etfs", action="store_true")
    parser.add_argument("--timeout", type=float, default=20.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    listings = fetch_us_listed_universe(
        include_etfs=not args.exclude_etfs,
        timeout=args.timeout,
    )
    observed = datetime.now(timezone.utc)
    observed_at = observed.isoformat()
    payload = {
        "schema_version": 1,
        "observed_at": observed_at,
        "source": "nasdaq_trader_symbol_directory",
        "count": len(listings),
        "symbols": [listing.project_symbol for listing in listings],
        "listings": [asdict(listing) for listing in listings],
    }
    _atomic_json(args.output, payload)
    if args.manifest:
        DataPlaneManifest(args.manifest).mark_success(
            "equity_universe",
            observed_at=observed,
            source="nasdaq_trader_symbol_directory",
            detail=f"{len(listings)} current listings",
            metadata={"count": len(listings), "output": str(args.output)},
        )
    print(json.dumps({"status": "ok", "count": len(listings), "observed_at": observed_at, "output": str(args.output)}))
    return 0


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
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
