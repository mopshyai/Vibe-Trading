#!/usr/bin/env python3
"""Replay point-in-time U.S. options selections from the DuckDB research store."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent"
if str(AGENT) not in sys.path:
    sys.path.insert(0, str(AGENT))

from src.options_market.replay import ReplayConfig, replay_many  # noqa: E402
from src.options_market.store import OptionsResearchStore  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Point-in-time options research replay")
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--symbols", required=True, type=Path, help="JSON list or newline-delimited symbols")
    parser.add_argument("--times", required=True, type=Path, help="JSON list of timezone-aware research timestamps")
    parser.add_argument("--evaluation-as-of", help="Optional timezone-aware timestamp used only for outcome labeling")
    parser.add_argument("--target-profit-pct", type=float, default=300.0)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    symbols = _symbols(args.symbols)
    times = _times(args.times)
    evaluation = _timestamp(args.evaluation_as_of) if args.evaluation_as_of else None
    with OptionsResearchStore(args.store) as store:
        result = replay_many(
            store,
            symbols,
            times,
            evaluation_as_of=evaluation,
            config=ReplayConfig(target_profit_pct=args.target_profit_pct),
        )
    rendered = json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False, default=str) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


def _symbols(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = [line.strip() for line in text.splitlines()]
    if isinstance(payload, dict):
        payload = payload.get("symbols", [])
    if not isinstance(payload, list):
        raise ValueError("symbols must be a JSON list, {symbols:[...]}, or newline-delimited file")
    return list(dict.fromkeys(str(value).strip().upper() for value in payload if str(value).strip()))


def _times(path: Path) -> list[datetime]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("times JSON must contain a list")
    return [_timestamp(value) for value in payload]


def _timestamp(value: object) -> datetime:
    text = str(value or "").strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include timezone: {value!r}")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
