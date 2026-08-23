#!/usr/bin/env python3
"""Apply the retrospective long-option execution model to an experiment JSON.

The command reads a local historical experiment and local OptionsResearchStore.
It performs no provider network request and no broker operation. `--plan-only`
reads the experiment contract but does not open the research store.
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

from src.options_market.historical_execution import HistoricalExecutionConfig  # noqa: E402
from src.options_market.historical_execution_experiment import apply_execution_model_to_experiment  # noqa: E402
from src.options_market.store import OptionsResearchStore  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply historical limit-order execution realism")
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--experiment-json", required=True, type=Path)
    parser.add_argument("--latency-seconds", type=float, default=1.0)
    parser.add_argument("--max-wait-seconds", type=float, default=120.0)
    parser.add_argument("--max-chase-pct", type=float, default=0.0)
    parser.add_argument("--max-spread-pct", type=float, default=20.0)
    parser.add_argument(
        "--fill-at-trigger-ask",
        action="store_true",
        help="Less-conservative mode: fill at displayed trigger ask instead of submitted limit",
    )
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    experiment = _experiment(args.experiment_json)
    config = HistoricalExecutionConfig(
        latency_seconds=args.latency_seconds,
        max_wait_seconds=args.max_wait_seconds,
        max_chase_pct=args.max_chase_pct,
        max_spread_pct=args.max_spread_pct,
        fill_at_limit=not args.fill_at_trigger_ask,
    )
    config.validate()
    selected = experiment.get("selected_candidates")
    if not isinstance(selected, list):
        raise ValueError("experiment selected_candidates list is required")

    plan = {
        "mode": "historical_execution_adjustment_plan",
        "source_experiment_id": experiment.get("experiment_id"),
        "experiment_json": str(args.experiment_json),
        "store": str(args.store),
        "selected_candidate_count": len(selected),
        "execution_config": {
            "latency_seconds": config.latency_seconds,
            "max_wait_seconds": config.max_wait_seconds,
            "max_chase_pct": config.max_chase_pct,
            "max_spread_pct": config.max_spread_pct,
            "fill_at_limit": config.fill_at_limit,
        },
        "provider_network_requests": False,
        "broker_mutation": False,
        "evaluation_only": True,
    }
    if args.plan_only:
        return _emit(plan, args.output)
    if not args.store.exists():
        raise FileNotFoundError(f"research store does not exist: {args.store}")

    with OptionsResearchStore(args.store) as store:
        report = apply_execution_model_to_experiment(store, experiment, config=config)
    report["plan"] = plan
    return _emit(report, args.output)


def _experiment(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("experiment JSON must contain an object")
    return dict(payload)


def _emit(payload: Mapping[str, Any], output: Path | None) -> int:
    rendered = json.dumps(dict(payload), indent=2, ensure_ascii=False, allow_nan=False, default=str) + "\n"
    if output is None:
        print(rendered, end="")
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
