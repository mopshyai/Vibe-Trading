#!/usr/bin/env python3
"""Offline/batch runner for the U.S. options market research pipeline.

The runner deliberately separates data acquisition from signal generation. Feed
it a point-in-time OHLCV CSV exported from any bulk provider; it can score many
thousands of U.S. listings without issuing one HTTP request per symbol.

Required CSV columns: symbol,date,open,high,low,close,volume
Optional --options-json: mapping of symbol -> list of option candidate dicts
produced by the options-opportunity layer.

Research only. This script never connects to a broker or places orders.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent"
if str(AGENT) not in sys.path:
    sys.path.insert(0, str(AGENT))

from src.options_market import (  # noqa: E402
    ChartScreenConfig,
    OptionsMarketPipelineConfig,
    combine_rankings,
    scan_market_frames,
)


def load_frames(path: Path) -> dict[str, pd.DataFrame]:
    """Load long-form point-in-time OHLCV CSV into symbol-indexed frames."""
    frame = pd.read_csv(path)
    required = {"symbol", "date", "open", "high", "low", "close", "volume"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"bars CSV is missing required columns: {sorted(missing)}")
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["symbol", "date"])

    output: dict[str, pd.DataFrame] = {}
    for symbol, group in frame.groupby("symbol", sort=False):
        bars = group.set_index("date")[["open", "high", "low", "close", "volume"]].sort_index()
        output[str(symbol).strip().upper()] = bars
    return output


def load_options(path: Path | None) -> dict[str, list[dict]]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("options JSON must be an object mapping symbol to candidate list")
    return {
        str(symbol).strip().upper(): list(candidates)
        for symbol, candidates in payload.items()
        if isinstance(candidates, list)
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cross-sectional U.S. options research scan")
    parser.add_argument("--bars-csv", required=True, type=Path, help="Long-form OHLCV CSV")
    parser.add_argument("--options-json", type=Path, help="Optional precomputed option candidates")
    parser.add_argument("--output", type=Path, help="Write result JSON to this path")
    parser.add_argument("--chart-top-n", type=int, default=200)
    parser.add_argument("--final-top-n", type=int, default=10)
    parser.add_argument("--target-profit-pct", type=float, default=300.0)
    parser.add_argument("--min-price", type=float, default=3.0)
    parser.add_argument("--min-dollar-volume", type=float, default=20_000_000.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    frames = load_frames(args.bars_csv)
    chart = scan_market_frames(
        frames,
        ChartScreenConfig(
            min_price=args.min_price,
            min_avg_dollar_volume=args.min_dollar_volume,
            top_n=args.chart_top_n,
        ),
    )

    options = load_options(args.options_json)
    if options:
        result = {
            "chart_stage": chart,
            "final_stage": combine_rankings(
                chart["candidates"],
                options,
                config=OptionsMarketPipelineConfig(
                    target_profit_pct=args.target_profit_pct,
                    final_top_n=args.final_top_n,
                ),
            ),
        }
    else:
        result = {"chart_stage": chart}

    rendered = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
