#!/usr/bin/env python3
"""Backfill point-in-time OPRA option metadata into OptionsResearchStore.

This command may issue paid historical data-provider requests. It never runs from
the hosted trading worker and supports --plan-only so request scope can be
reviewed before any Databento call is made.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent"
if str(AGENT) not in sys.path:
    sys.path.insert(0, str(AGENT))

from src.options_market.databento_adapter import DatabentoHistoricalConfig  # noqa: E402
from src.options_market.databento_metadata import DatabentoOptionMetadataAdapter  # noqa: E402
from src.options_market.store import OptionsResearchStore  # noqa: E402

UTC = timezone.utc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill Databento OPRA option metadata")
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--underlyings-file", required=True, type=Path)
    parser.add_argument("--start", required=True, help="Timezone-aware ISO timestamp")
    parser.add_argument("--end", required=True, help="Timezone-aware ISO timestamp")
    parser.add_argument("--definitions", action="store_true")
    parser.add_argument("--open-interest", action="store_true")
    parser.add_argument("--daily-volume", action="store_true")
    parser.add_argument("--chunk-size", type=int, default=2000)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    start = _aware(args.start, "start")
    end = _aware(args.end, "end")
    if end <= start:
        raise ValueError("end must be after start")
    if args.chunk_size < 1:
        raise ValueError("chunk-size must be positive")
    underlyings = _underlyings(args.underlyings_file)

    selected = {
        "definitions": bool(args.definitions),
        "open_interest": bool(args.open_interest),
        "daily_volume": bool(args.daily_volume),
    }
    if not any(selected.values()):
        selected = {key: True for key in selected}

    plan = {
        "mode": "databento_opra_metadata_backfill",
        "plan_only": bool(args.plan_only),
        "store": str(args.store),
        "underlyings": underlyings,
        "underlying_count": len(underlyings),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "selected": selected,
        "dataset": "OPRA.PILLAR",
        "request_schemas": [
            schema
            for enabled, schema in (
                (selected["definitions"], "definition"),
                (selected["open_interest"], "statistics"),
                (selected["daily_volume"], "ohlcv-1d"),
            )
            if enabled
        ],
        "provider_requests_may_incur_charges": True,
        "broker_mutation": False,
        "historical_iv_derived": False,
    }
    if args.plan_only:
        return _emit(plan, args.output)

    api_key = str(os.environ.get("DATABENTO_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("DATABENTO_API_KEY is required for a non-plan metadata backfill")

    config = DatabentoHistoricalConfig(chunk_size=args.chunk_size)
    adapter = DatabentoOptionMetadataAdapter(api_key=api_key, config=config)
    counts = {"definitions": 0, "open_interest": 0, "daily_volume": 0}
    per_underlying: list[dict[str, Any]] = []
    args.store.parent.mkdir(parents=True, exist_ok=True)
    with OptionsResearchStore(args.store) as store:
        for underlying in underlyings:
            row = {"underlying": underlying, "definitions": 0, "open_interest": 0, "daily_volume": 0}
            if selected["definitions"]:
                row["definitions"] = adapter.ingest_option_definitions(
                    underlying, start=start, end=end, store=store
                )
                counts["definitions"] += int(row["definitions"])
            if selected["open_interest"]:
                row["open_interest"] = adapter.ingest_option_open_interest(
                    underlying, start=start, end=end, store=store
                )
                counts["open_interest"] += int(row["open_interest"])
            if selected["daily_volume"]:
                row["daily_volume"] = adapter.ingest_option_daily_volume(
                    underlying, start=start, end=end, store=store
                )
                counts["daily_volume"] += int(row["daily_volume"])
            per_underlying.append(row)
        store_counts = store.counts()

    result = {
        **plan,
        "plan_only": False,
        "ingested": counts,
        "per_underlying": per_underlying,
        "store_counts": store_counts,
        "completed_at": datetime.now(UTC).isoformat(),
        "warning": (
            "Historical metadata only. OPRA open interest/daily volume/definitions do not supply a complete historical IV/Greeks surface."
        ),
    }
    return _emit(result, args.output)


def _underlyings(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, Mapping):
        values = payload.get("symbols") or payload.get("underlyings")
    elif isinstance(payload, list):
        values = payload
    else:
        values = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not isinstance(values, list):
        raise ValueError("underlyings file must be a JSON list, object with symbols/underlyings, or one per line")
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        symbol = str(value or "").strip().upper()
        if symbol.endswith(".US"):
            symbol = symbol[:-3]
        if symbol and symbol not in seen:
            seen.add(symbol)
            output.append(symbol)
    if not output:
        raise ValueError("underlyings file contains no symbols")
    return output


def _aware(value: object, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return parsed.astimezone(UTC)


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
