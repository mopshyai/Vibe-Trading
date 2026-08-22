#!/usr/bin/env python3
"""Select an empirically supported long-option payoff target from labeled outcomes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent"
if str(AGENT) not in sys.path:
    sys.path.insert(0, str(AGENT))

from src.options_market.payoff import PayoffPolicyConfig, evaluate_payoff_targets  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate 2x/3x/4x-style option payoff targets")
    parser.add_argument("--outcomes-json", required=True, type=Path, help="JSON list of labeled historical outcomes")
    parser.add_argument("--targets", default="100,200,300", help="Comma-separated profit targets in percent")
    parser.add_argument("--min-samples", type=int, default=50)
    parser.add_argument("--confidence", type=float, default=0.90)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = json.loads(args.outcomes_json.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("outcomes JSON must contain a list")
    targets = tuple(float(token.strip()) for token in args.targets.split(",") if token.strip())
    result = evaluate_payoff_targets(
        [row for row in payload if isinstance(row, dict)],
        config=PayoffPolicyConfig(
            target_profit_pcts=targets,
            min_samples=args.min_samples,
            confidence_level=args.confidence,
        ),
    )
    rendered = json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
