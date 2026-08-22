#!/usr/bin/env python3
"""Backfill point-in-time U.S. equity/options research data from Databento.

The API key is read by ``DatabentoHistoricalAdapter`` from ``DATABENTO_API_KEY``.
This CLI deliberately has no --api-key argument so secrets do not become shell
history or process-list data.

Historical downloads can incur provider charges. Use ``--plan-only`` first to
inspect the requested windows before executing a backfill.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from typing import Iterable

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent"
if str(AGENT) not in sys.path:
    sys.path.insert(0, str(AGENT))

from src.options_market import DatabentoHistoricalAdapter, OptionsResearchStore  # noqa: E402

UTC = timezone.utc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill point-in-time U.S. market research data")
    parser.add_argument("--store", required=True, type=Path, help="DuckDB research-store path")
    parser.add_argument("--start", required=True, help="Inclusive UTC/date-like start")
    parser.add_argument("--end", required=True, help="Exclusive/end UTC/date-like end")
    parser.add_argument("--equity", action="store_true", help="Backfill consolidated U.S. daily equity bars")
    parser.add_argument(
        "--equity-symbols",
        type=Path,
        help="Optional JSON/newline symbol list; omit to request Databento ALL_SYMBOLS",
    )
    parser.add_argument(
        "--options-underlyings",
        type=Path,
        help="JSON/newline underlying list for OPRA CBBO backfill",
    )
    parser.add_argument("--equity-chunk-days", type=int, default=31)
    parser.add_argument("--options-chunk-days", type=int, default=5)
    parser.add_argument("--plan-only", action="store_true", help="Print windows without making provider requests")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    start = _timestamp(args.start, "start")
    end = _timestamp(args.end, "end")
    if end <= start:
        raise ValueError("end must be after start")
    if not args.equity and args.options_underlyings is None:
        raise ValueError("select --equity and/or provide --options-underlyings")
    if args.equity_chunk_days < 1 or args.options_chunk_days < 1:
        raise ValueError("chunk days must be positive")

    equity_symbols = _symbols_file(args.equity_symbols) if args.equity_symbols else None
    option_underlyings = _symbols_file(args.options_underlyings) if args.options_underlyings else []
    plan = {
        "store": str(args.store),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "equity": {
            "enabled": bool(args.equity),
            "symbols": "ALL_SYMBOLS" if equity_symbols is None else len(equity_symbols),
            "windows": [
                {"start": left.isoformat(), "end": right.isoformat()}
                for left, right in _windows(start, end, args.equity_chunk_days)
            ],
        },
        "options": {
            "underlyings": option_underlyings,
            "windows": [
                {"start": left.isoformat(), "end": right.isoformat()}
                for left, right in _windows(start, end, args.options_chunk_days)
            ] if option_underlyings else [],
        },
        "warning": "Historical provider downloads may incur charges. DATABENTO_API_KEY is read only at execution time.",
    }
    if args.plan_only:
        print(json.dumps(plan, indent=2, ensure_ascii=False))
        return 0

    adapter = DatabentoHistoricalAdapter()
    args.store.parent.mkdir(parents=True, exist_ok=True)
    ingested_equity = 0
    ingested_options = 0
    with OptionsResearchStore(args.store) as store:
        if args.equity:
            for left, right in _windows(start, end, args.equity_chunk_days):
                ingested_equity += adapter.ingest_equity_daily_bars(
                    store,
                    start=left,
                    end=right,
                    symbols=equity_symbols,
                )
                print(
                    json.dumps(
                        {
                            "stage": "equity",
                            "window_start": left.isoformat(),
                            "window_end": right.isoformat(),
                            "rows_total": ingested_equity,
                        }
                    ),
                    flush=True,
                )

        if option_underlyings:
            for left, right in _windows(start, end, args.options_chunk_days):
                ingested_options += adapter.ingest_option_cbbo(
                    store,
                    underlyings=option_underlyings,
                    start=left,
                    end=right,
                )
                print(
                    json.dumps(
                        {
                            "stage": "options",
                            "window_start": left.isoformat(),
                            "window_end": right.isoformat(),
                            "rows_total": ingested_options,
                        }
                    ),
                    flush=True,
                )
        counts = store.counts()

    print(
        json.dumps(
            {
                "status": "complete",
                "ingested_equity_rows": ingested_equity,
                "ingested_option_rows": ingested_options,
                "store_counts": counts,
            },
            indent=2,
        )
    )
    return 0


def _timestamp(value: object, name: str) -> datetime:
    parsed = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(parsed):
        raise ValueError(f"{name} must be a valid timestamp")
    return pd.Timestamp(parsed).to_pydatetime().astimezone(UTC)


def _windows(start: datetime, end: datetime, days: int) -> Iterable[tuple[datetime, datetime]]:
    current = start
    delta = timedelta(days=days)
    while current < end:
        right = min(end, current + delta)
        yield current, right
        current = right


def _symbols_file(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = [line.strip() for line in text.splitlines()]
    if isinstance(payload, dict):
        payload = payload.get("symbols", [])
    if not isinstance(payload, list):
        raise ValueError(f"{path} must contain a JSON list, {{symbols:[...]}}, or newline list")
    output: list[str] = []
    seen: set[str] = set()
    for value in payload:
        symbol = str(value or "").strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            output.append(symbol)
    return output


if __name__ == "__main__":
    raise SystemExit(main())
