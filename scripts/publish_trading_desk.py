#!/usr/bin/env python3
"""Publish one personal-analysis cycle into the durable Trading Desk store.

This is a local state write only. It never connects to a broker and never places
an order. It is useful both for one-shot analysis and as the publication step of
a supervised continuous runtime.
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
from src.trading_platform import (  # noqa: E402
    DataQualitySummary,
    PlatformEnvironment,
    RiskSummary,
    SystemIdentity,
    TradingPlatformService,
    TradingPlatformStore,
    evaluate_data_freshness,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish a Trading Desk snapshot")
    parser.add_argument("--cycle-json", required=True, type=Path)
    parser.add_argument("--store", type=Path, default=None)
    parser.add_argument("--environment", choices=[item.value for item in PlatformEnvironment], default="research")
    parser.add_argument(
        "--health-json",
        type=Path,
        help="Component observations or a DataPlaneManifest JSON file",
    )
    parser.add_argument("--risk-json", type=Path, help="RiskSummary-compatible JSON")
    parser.add_argument("--commit-sha")
    parser.add_argument("--platform-version", default="personal-trading-platform-v0.1")
    return parser.parse_args()


def _mapping_file(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"{path} must contain a JSON object")
    return dict(payload)


def _health_components(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    nested = payload.get("components")
    source = nested if isinstance(nested, Mapping) else payload
    return {
        str(name): dict(value)
        for name, value in source.items()
        if isinstance(value, Mapping)
    }


def main() -> int:
    args = parse_args()
    cycle = _mapping_file(args.cycle_json)
    health_payload = _mapping_file(args.health_json)
    risk_payload = _mapping_file(args.risk_json)

    if health_payload:
        quality = evaluate_data_freshness(_health_components(health_payload))
    else:
        quality = DataQualitySummary(
            healthy=False,
            blocking_reasons=["health_observations_not_supplied"],
        )

    risk = RiskSummary.model_validate(risk_payload) if risk_payload else RiskSummary()
    system = SystemIdentity(
        platform_version=args.platform_version,
        commit_sha=str(args.commit_sha or "").strip() or None,
    )
    store_path = args.store or (get_runtime_root() / "trading-platform.duckdb")
    store_path.parent.mkdir(parents=True, exist_ok=True)

    with TradingPlatformStore(store_path) as store:
        service = TradingPlatformService(
            store=store,
            environment=PlatformEnvironment(args.environment),
            system=system,
        )
        snapshot = service.publish_personal_cycle(
            cycle,
            data_quality=quality,
            risk=risk,
        )

    print(json.dumps(snapshot.model_dump(mode="json"), indent=2, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
