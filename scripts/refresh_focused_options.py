#!/usr/bin/env python3
"""Refresh current option candidates for the continuous scanner's focus list.

This path is read-only. It uses the existing Alpaca current-options reader and
writes a symbol -> candidate-list JSON artifact that the continuous analyzer can
hot-reload. The selected feed is always recorded; ``indicative`` never becomes
execution-grade by omission.

Each successful focused refresh also reads a current underlying quote, so the
optional data-plane manifest updates both ``equity_market`` and
``options_market`` freshness. Historical backfills intentionally do not update
these current-data components.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent"
if str(AGENT) not in sys.path:
    sys.path.insert(0, str(AGENT))

from src.options_market.alpaca_current import (  # noqa: E402
    AlpacaCurrentOptionsConfig,
    AlpacaCurrentOptionsReader,
)
from src.trading_platform import DataPlaneManifest  # noqa: E402
from src.trading_platform.runtime_config import load_alpaca_runtime_config  # noqa: E402

UTC = timezone.utc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh focused current option candidates")
    parser.add_argument("--focus-json", required=True, type=Path, help="Chart candidates or analysis JSON")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--manifest", type=Path, help="Optional data-plane freshness manifest")
    parser.add_argument("--feed", choices=["indicative", "opra"], default="indicative")
    parser.add_argument("--top-n", type=int, default=50)
    parser.add_argument("--target-profit-pct", type=float, default=300.0)
    parser.add_argument("--min-dte", type=int, default=7)
    parser.add_argument("--max-dte", type=int, default=60)
    parser.add_argument("--max-results-per-symbol", type=int, default=10)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 1 <= args.top_n <= 200:
        raise ValueError("top-n must be between 1 and 200")
    focus = _focus_rows(args.focus_json)[: args.top_n]
    reader = AlpacaCurrentOptionsReader(
        alpaca_config=load_alpaca_runtime_config(),
        config=AlpacaCurrentOptionsConfig(
            min_dte=args.min_dte,
            max_dte=args.max_dte,
            option_feed=args.feed,
            max_results=args.max_results_per_symbol,
        ),
    )
    observed_at = datetime.now(UTC)
    options: dict[str, list[dict[str, Any]]] = {}
    status: list[dict[str, Any]] = []

    for row in focus:
        symbol = str(row.get("symbol") or row.get("ticker") or "").strip().upper()
        direction = str(row.get("direction") or "").strip().lower()
        if not symbol or direction not in {"bullish", "bearish"}:
            continue
        try:
            result = reader.fetch_candidates(
                symbol,
                direction=direction,
                target_profit_pct=args.target_profit_pct,
                now=observed_at,
            )
            candidates = [dict(item) for item in result.get("candidates", []) if isinstance(item, Mapping)]
            options[symbol] = candidates
            status.append(
                {
                    "symbol": symbol,
                    "direction": direction,
                    "feed": result.get("feed"),
                    "execution_grade_feed": result.get("execution_grade_feed"),
                    "candidate_count": len(candidates),
                    "status": "ok",
                }
            )
        except Exception as exc:  # noqa: BLE001 - one symbol failure must not discard successful reads
            options[symbol] = []
            status.append(
                {
                    "symbol": symbol,
                    "direction": direction,
                    "feed": args.feed,
                    "execution_grade_feed": args.feed == "opra",
                    "candidate_count": 0,
                    "status": "error",
                    "error": str(exc),
                }
            )

    payload = {
        "schema_version": 1,
        "observed_at": observed_at.isoformat(),
        "source": "alpaca_current_options",
        "feed": args.feed,
        "execution_grade_feed": args.feed == "opra",
        "focus_count": len(focus),
        "symbols_refreshed": len(options),
        "status": status,
        "options": options,
    }
    # The continuous scanner expects the top-level mapping. Keep a small sidecar
    # metadata file so provenance is not lost while preserving that simple API.
    _atomic_json(args.output, options)
    _atomic_json(args.output.with_suffix(args.output.suffix + ".meta.json"), payload)

    if args.manifest:
        _publish_manifest(
            DataPlaneManifest(args.manifest),
            status=status,
            feed=args.feed,
            observed_at=observed_at,
            focus_count=len(focus),
            output=args.output,
        )

    print(json.dumps({key: value for key, value in payload.items() if key != "options"}, ensure_ascii=False))
    return 0


def _publish_manifest(
    manifest: DataPlaneManifest,
    *,
    status: list[dict[str, Any]],
    feed: str,
    observed_at: datetime,
    focus_count: int,
    output: Path,
) -> None:
    errors = [row for row in status if row.get("status") == "error"]
    successes = len(status) - len(errors)
    source = f"alpaca:{feed}"
    if not status:
        message = "no valid focused symbols were available to refresh"
        manifest.mark_error("equity_market", message, source=source, observed_at=observed_at)
        manifest.mark_error("options_market", message, source=source, observed_at=observed_at)
        return
    if len(errors) == len(status):
        message = f"all {len(status)} focused market-data refreshes failed"
        manifest.mark_error("equity_market", message, source=source, observed_at=observed_at)
        manifest.mark_error("options_market", message, source=source, observed_at=observed_at)
        return

    common_metadata = {
        "feed": feed,
        "focus_count": focus_count,
        "symbols_attempted": len(status),
        "successes": successes,
        "errors": len(errors),
        "output": str(output),
    }
    manifest.mark_success(
        "equity_market",
        observed_at=observed_at,
        source=source,
        detail=f"current underlying quotes refreshed for {successes}/{len(status)} focus symbols",
        metadata=common_metadata,
    )
    manifest.mark_success(
        "options_market",
        observed_at=observed_at,
        source=source,
        detail=f"current option surfaces refreshed for {successes}/{len(status)} focus symbols",
        metadata={**common_metadata, "execution_grade_feed": feed == "opra"},
    )


def _focus_rows(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, Mapping):
        for key in ("candidates", "chart_candidates"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [dict(row) for row in rows if isinstance(row, Mapping)]
        for key in ("chart_stage", "state"):
            nested = payload.get(key)
            if isinstance(nested, Mapping):
                rows = nested.get("candidates") or nested.get("chart_candidates")
                if isinstance(rows, list):
                    return [dict(row) for row in rows if isinstance(row, Mapping)]
    if isinstance(payload, list):
        return [dict(row) for row in payload if isinstance(row, Mapping)]
    raise ValueError("focus JSON does not contain a candidate list")


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
