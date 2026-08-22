#!/usr/bin/env python3
"""Refresh focused Alpaca news, persist it, and publish catalyst scores.

The output is a symbol -> directional catalyst score mapping compatible with the
continuous options analyzer. News rows are stored with publication/update times
before scoring, allowing the same events to be queried point-in-time in replay.

This script is read-only with respect to Alpaca and never touches broker orders.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
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

from src.options_market.alpaca_news import AlpacaNewsConfig, AlpacaNewsReader  # noqa: E402
from src.options_market.catalyst import score_catalysts  # noqa: E402
from src.options_market.catalyst_store import CatalystEventStore  # noqa: E402
from src.trading_platform import DataPlaneManifest  # noqa: E402

UTC = timezone.utc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh focused news/catalyst scores")
    parser.add_argument("--focus-json", required=True, type=Path)
    parser.add_argument("--store", required=True, type=Path, help="Catalyst DuckDB path")
    parser.add_argument("--output", required=True, type=Path, help="Symbol -> score JSON")
    parser.add_argument("--manifest", type=Path, help="Optional data-plane freshness manifest")
    parser.add_argument("--lookback-hours", type=float, default=72.0)
    parser.add_argument("--score-window-hours", type=float, default=240.0)
    parser.add_argument("--top-n", type=int, default=50)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 1 <= args.top_n <= 200:
        raise ValueError("top-n must be between 1 and 200")
    if args.score_window_hours <= 0:
        raise ValueError("score-window-hours must be positive")

    focus = _focus_rows(args.focus_json)[: args.top_n]
    original_to_clean = {
        str(row.get("symbol") or row.get("ticker") or "").strip().upper():
        _clean_symbol(row.get("symbol") or row.get("ticker"))
        for row in focus
        if _clean_symbol(row.get("symbol") or row.get("ticker"))
    }
    clean_symbols = list(dict.fromkeys(original_to_clean.values()))
    now = datetime.now(UTC)
    fetch_error: str | None = None
    fetched_rows: list[dict[str, Any]] = []

    try:
        fetched_rows = AlpacaNewsReader(
            config=AlpacaNewsConfig(lookback_hours=args.lookback_hours)
        ).fetch(clean_symbols, now=now)
    except Exception as exc:  # noqa: BLE001 - stale store may still support a degraded score
        fetch_error = str(exc)

    args.store.parent.mkdir(parents=True, exist_ok=True)
    with CatalystEventStore(args.store) as store:
        ingested = store.ingest(fetched_rows) if fetched_rows else 0
        events = store.catalyst_events_asof(
            as_of=now,
            symbols=clean_symbols,
            start=now - timedelta(hours=args.score_window_hours),
        )
        scored = score_catalysts(events, as_of=now)
        stored_count = store.count()

    scores: dict[str, float] = {}
    details: dict[str, Any] = {}
    focus_by_original = {
        str(row.get("symbol") or row.get("ticker") or "").strip().upper(): row
        for row in focus
    }
    for original, clean in original_to_clean.items():
        row = focus_by_original.get(original, {})
        direction = str(row.get("direction") or "").strip().lower()
        context = scored.get(clean)
        if not context:
            continue
        details[original] = context
        if direction == "bullish" and context.get("direction") == "bullish":
            scores[original] = float(context.get("bullish_score") or 0.0)
        elif direction == "bearish" and context.get("direction") == "bearish":
            scores[original] = float(context.get("bearish_score") or 0.0)

    meta = {
        "schema_version": 1,
        "observed_at": now.isoformat(),
        "source": "alpaca_news",
        "focus_count": len(focus),
        "clean_symbol_count": len(clean_symbols),
        "fetched_event_rows": len(fetched_rows),
        "ingested_event_rows": ingested,
        "stored_event_rows": stored_count,
        "aligned_score_count": len(scores),
        "fetch_error": fetch_error,
        "details": details,
    }
    _atomic_json(args.output, scores)
    _atomic_json(args.output.with_suffix(args.output.suffix + ".meta.json"), meta)

    if args.manifest:
        manifest = DataPlaneManifest(args.manifest)
        if fetch_error:
            manifest.mark_error(
                "catalysts",
                fetch_error,
                source="alpaca_news",
                observed_at=now,
            )
        else:
            manifest.mark_success(
                "catalysts",
                observed_at=now,
                source="alpaca_news",
                detail=f"{len(fetched_rows)} event-symbol rows fetched; {len(scores)} directional scores aligned",
                metadata={
                    "focus_count": len(focus),
                    "fetched_event_rows": len(fetched_rows),
                    "aligned_score_count": len(scores),
                    "store": str(args.store),
                    "output": str(args.output),
                },
            )

    print(json.dumps({key: value for key, value in meta.items() if key != "details"}, ensure_ascii=False))
    return 0 if fetch_error is None else 2


def _clean_symbol(value: object) -> str:
    symbol = str(value or "").strip().upper()
    return symbol[:-3] if symbol.endswith(".US") else symbol


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
