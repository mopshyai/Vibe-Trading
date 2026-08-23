#!/usr/bin/env python3
"""Refresh current option candidates and bounded volatility-surface context.

This path is read-only. It uses Alpaca current-options GETs and writes the same
symbol -> candidate-list JSON artifact expected by the continuous analyzer.
For only the highest-priority focus names it also fetches both call/put surfaces
and annotates candidates with provider-independent skew, term-structure,
expected-move and relative-efficiency context.
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
from src.options_market.alpaca_surface import fetch_alpaca_volatility_surface  # noqa: E402
from src.options_market.surface import contract_surface_context  # noqa: E402
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
    parser.add_argument(
        "--surface-top-n",
        type=int,
        default=20,
        help="Fetch full call+put volatility surfaces only for this many highest-priority focus names",
    )
    parser.add_argument("--target-profit-pct", type=float, default=300.0)
    parser.add_argument("--min-dte", type=int, default=7)
    parser.add_argument("--max-dte", type=int, default=60)
    parser.add_argument("--max-results-per-symbol", type=int, default=10)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 1 <= args.top_n <= 200:
        raise ValueError("top-n must be between 1 and 200")
    if not 0 <= args.surface_top_n <= args.top_n:
        raise ValueError("surface-top-n must be between 0 and top-n")
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
    surfaces: dict[str, dict[str, Any]] = {}
    status: list[dict[str, Any]] = []

    for index, row in enumerate(focus):
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
            surface_status = "skipped"
            surface_report: dict[str, Any] | None = None
            if index < args.surface_top_n and candidates:
                try:
                    surface_report = fetch_alpaca_volatility_surface(
                        reader,
                        symbol,
                        now=observed_at,
                        realized_vol_pct=_number(row.get("realized_vol20_pct")),
                    )
                    surfaces[symbol] = surface_report
                    surface_status = str(surface_report.get("status") or "unknown")
                    for candidate in candidates:
                        context = contract_surface_context(candidate, surface_report)
                        candidate["surface_context"] = context
                        candidate["surface_efficiency_score"] = context.get("surface_efficiency_score")
                        candidate["surface_iv_percentile"] = context.get("surface_iv_percentile")
                        candidate["surface_required_move_ratio"] = context.get(
                            "required_move_vs_surface_expected_move"
                        )
                except Exception as exc:  # noqa: BLE001 - candidate refresh remains usable
                    surface_status = "error"
                    surfaces[symbol] = {
                        "status": "error",
                        "underlying": symbol,
                        "observed_at": observed_at.isoformat(),
                        "error": f"{type(exc).__name__}: {exc}",
                    }

            options[symbol] = candidates
            surface_term_state = None
            surface_skew_state = None
            surface_iv_state = None
            if isinstance(surface_report, Mapping):
                surface_term_state = _nested(surface_report, "term_structure", "state")
                surface_skew_state = _nested(surface_report, "skew", "state")
                surface_iv_state = _nested(surface_report, "implied_vs_realized", "state")
            status.append(
                {
                    "symbol": symbol,
                    "direction": direction,
                    "feed": result.get("feed"),
                    "execution_grade_feed": result.get("execution_grade_feed"),
                    "candidate_count": len(candidates),
                    "surface_status": surface_status,
                    "term_structure": surface_term_state,
                    "skew": surface_skew_state,
                    "implied_vs_realized": surface_iv_state,
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
                    "surface_status": "skipped",
                    "status": "error",
                    "error": str(exc),
                }
            )

    surface_output = args.output.with_suffix(args.output.suffix + ".surface.json")
    payload = {
        "schema_version": 2,
        "observed_at": observed_at.isoformat(),
        "source": "alpaca_current_options",
        "feed": args.feed,
        "execution_grade_feed": args.feed == "opra",
        "focus_count": len(focus),
        "symbols_refreshed": len(options),
        "surface_top_n": args.surface_top_n,
        "surface_symbols_refreshed": len(surfaces),
        "surface_output": str(surface_output),
        "status": status,
        "options": options,
    }
    # Preserve the scanner's simple top-level candidate mapping. Provenance and
    # surface detail live in sidecars rather than changing the analyzer API.
    _atomic_json(args.output, options)
    _atomic_json(surface_output, surfaces)
    _atomic_json(args.output.with_suffix(args.output.suffix + ".meta.json"), payload)

    if args.manifest:
        _publish_manifest(
            DataPlaneManifest(args.manifest),
            status=status,
            feed=args.feed,
            observed_at=observed_at,
            focus_count=len(focus),
            surface_count=len(surfaces),
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
    surface_count: int,
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
        "surface_symbols_refreshed": surface_count,
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
        detail=(
            f"directional options refreshed for {successes}/{len(status)} focus symbols; "
            f"full call/put surfaces enriched for {surface_count}"
        ),
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


def _nested(value: Mapping[str, Any], key: str, child: str) -> Any:
    nested = value.get(key)
    return nested.get(child) if isinstance(nested, Mapping) else None


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number == number and abs(number) != float("inf") else None


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
