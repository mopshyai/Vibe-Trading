#!/usr/bin/env python3
"""Read-only operational preflight for the personal Trading Desk."""

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
from src.trading_platform.preflight import run_platform_preflight  # noqa: E402
from src.trading_platform.runtime_config import load_alpaca_runtime_config  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check Trading Desk operational readiness")
    parser.add_argument("--store", type=Path, default=None)
    parser.add_argument("--execution-input-json", type=Path, help="Optional candidate + EV/walk-forward/risk bundle")
    parser.add_argument("--contract", help="Exact OCC option contract to probe with OPRA")
    parser.add_argument("--max-quote-age-seconds", type=float, default=90.0)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = _mapping_file(args.execution_input_json)
    store_path = args.store or (get_runtime_root() / "trading-platform.duckdb")
    result = run_platform_preflight(
        store_path=store_path,
        execution_input=payload,
        option_contract=args.contract,
        alpaca_config=load_alpaca_runtime_config(),
        max_quote_age_seconds=args.max_quote_age_seconds,
    )
    rendered = json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False, default=str) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if result.get("platform_operational") else 2


def _mapping_file(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("execution input JSON must contain an object")
    return dict(payload)


if __name__ == "__main__":
    raise SystemExit(main())
