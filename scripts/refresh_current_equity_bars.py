#!/usr/bin/env python3
"""Increment the current edge of the whole-market PIT equity store via Alpaca.

This is read-only with respect to Alpaca. It fetches recent split-adjusted daily
bars for the configured U.S. universe in multi-symbol batches and appends them to
`OptionsResearchStore` with `available_at` equal to the actual refresh time.

It is not a historical-replay substitute: rows fetched today are not made
knowable in yesterday's replay. Use the explicit Databento backfill path for
historical point-in-time research.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent"
if str(AGENT) not in sys.path:
    sys.path.insert(0, str(AGENT))

from src.options_market.alpaca_equity_bulk import AlpacaBulkEquityReader  # noqa: E402
from src.options_market.store import OptionsResearchStore  # noqa: E402
from src.trading_platform import DataPlaneManifest  # noqa: E402
from src.trading_platform.runtime_config import load_alpaca_runtime_config  # noqa: E402

UTC = timezone.utc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh recent whole-market daily equity bars")
    parser.add_argument("--symbols-file", required=True, type=Path)
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--output", type=Path, help="Optional refresh metadata JSON")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--lookback-days", type=int, default=10)
    parser.add_argument("--feed", choices=["sip", "iex"], default=None)
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--max-latest-bar-age-days", type=float, default=5.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 1 <= args.lookback_days <= 60:
        raise ValueError("lookback-days must be between 1 and 60")
    if args.max_latest_bar_age_days <= 0:
        raise ValueError("max-latest-bar-age-days must be positive")
    symbols = _symbols(args.symbols_file)
    if not symbols:
        raise ValueError("symbols file contains no symbols")

    broker = load_alpaca_runtime_config()
    feed = args.feed or ("sip" if broker.feed == "sip" else "iex")
    observed_at = datetime.now(UTC)
    start = observed_at - timedelta(days=args.lookback_days)
    reader = AlpacaBulkEquityReader(
        alpaca_config=broker,
        symbol_batch_size=args.batch_size,
    )
    frame = reader.fetch_daily_bars(
        symbols,
        start=start,
        end=observed_at,
        feed=feed,
        adjustment="split",
        observed_at=observed_at,
    )

    args.store.parent.mkdir(parents=True, exist_ok=True)
    with OptionsResearchStore(args.store) as store:
        ingested = store.ingest_equity_bars(frame)
        counts = store.counts()

    latest_event = None
    latest_age_days = None
    if not frame.empty:
        latest_event = frame["event_ts"].max()
        latest_dt = latest_event.to_pydatetime() if hasattr(latest_event, "to_pydatetime") else latest_event
        if latest_dt.tzinfo is None:
            latest_dt = latest_dt.replace(tzinfo=UTC)
        latest_age_days = max(0.0, (observed_at - latest_dt.astimezone(UTC)).total_seconds() / 86400.0)

    healthy = bool(ingested > 0 and latest_age_days is not None and latest_age_days <= args.max_latest_bar_age_days)
    result = {
        "schema_version": 1,
        "observed_at": observed_at.isoformat(),
        "source": "alpaca_bulk_equity_daily",
        "feed": feed,
        "consolidated_us_feed": feed == "sip",
        "symbols_requested": len(symbols),
        "rows_fetched": int(len(frame)),
        "rows_ingested": int(ingested),
        "latest_bar_event_ts": latest_event.isoformat() if latest_event is not None else None,
        "latest_bar_age_days": None if latest_age_days is None else round(latest_age_days, 4),
        "healthy": healthy,
        "store_counts": counts,
        "warning": (
            "SIP is the consolidated U.S. exchange feed; IEX is one exchange and should be treated as degraded for whole-market volume/ranking."
        ),
    }

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.manifest:
        manifest = DataPlaneManifest(args.manifest)
        if healthy:
            manifest.mark_success(
                "equity_daily_bars",
                observed_at=observed_at,
                source=f"alpaca:{feed}:1D:split",
                detail=f"{ingested} recent daily bars ingested for {len(symbols)} requested symbols",
                metadata=result,
            )
        else:
            manifest.mark_error(
                "equity_daily_bars",
                "recent whole-market daily bars unavailable or too old",
                source=f"alpaca:{feed}:1D:split",
                observed_at=observed_at,
            )

    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if healthy else 2


def _symbols(path: Path) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    values = payload.get("symbols") if isinstance(payload, Mapping) else payload
    if not isinstance(values, list):
        raise ValueError("symbols file must be a JSON list or object containing a symbols list")
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        symbol = str(value or "").strip().upper()
        if symbol.endswith(".US"):
            symbol = symbol[:-3]
        if symbol and symbol not in seen:
            seen.add(symbol)
            output.append(symbol)
    return output


if __name__ == "__main__":
    raise SystemExit(main())
