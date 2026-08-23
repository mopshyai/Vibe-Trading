#!/usr/bin/env python3
"""Import licensed/local historical option IV/Greeks observations.

This command reads a local CSV or JSON export only. It performs no provider
network request and no broker operation. The canonical store uses IV as a
fraction; `--iv-unit percent` converts inputs such as 40.0 to 0.40 explicitly.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent"
if str(AGENT) not in sys.path:
    sys.path.insert(0, str(AGENT))

from src.options_market.historical_volatility_store import HistoricalOptionVolatilityStore  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import historical option IV/Greeks from a local export")
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--iv-unit", choices=["fraction", "percent"], default="fraction")
    parser.add_argument("--source", help="Override/add source provenance when input rows do not contain source")
    parser.add_argument("--model", help="Optional model/provider model label applied when missing")
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    frame = _load(args.input)
    if frame.empty:
        raise ValueError("input contains no observations")
    frame = _prepare(frame, iv_unit=args.iv_unit, source=args.source, model=args.model)

    plan = {
        "mode": "historical_option_volatility_import",
        "plan_only": bool(args.plan_only),
        "input": str(args.input),
        "store": str(args.store),
        "rows": int(len(frame)),
        "contracts": int(frame["contract_symbol"].astype(str).str.replace(" ", "", regex=False).nunique()),
        "iv_unit_input": args.iv_unit,
        "iv_unit_store": "fraction",
        "columns": list(frame.columns),
        "provider_network_requests": False,
        "broker_mutation": False,
    }
    if args.plan_only:
        return _emit(plan, args.output)

    args.store.parent.mkdir(parents=True, exist_ok=True)
    with HistoricalOptionVolatilityStore(args.store) as store:
        inserted = store.ingest(frame)
        counts = store.counts()
    result = {
        **plan,
        "plan_only": False,
        "inserted": inserted,
        "store_counts": counts,
    }
    return _emit(result, args.output)


def _load(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows: Any
    if isinstance(payload, Mapping):
        rows = payload.get("observations") or payload.get("rows")
    else:
        rows = payload
    if not isinstance(rows, list):
        raise ValueError("JSON input must be a list or object containing observations/rows")
    return pd.DataFrame([dict(row) for row in rows if isinstance(row, Mapping)])


def _prepare(
    frame: pd.DataFrame,
    *,
    iv_unit: str,
    source: str | None,
    model: str | None,
) -> pd.DataFrame:
    output = frame.copy()
    required = {"contract_symbol", "event_ts", "implied_volatility", "available_at"}
    missing = required.difference(output.columns)
    if missing:
        raise ValueError(f"input missing columns: {sorted(missing)}")
    output["implied_volatility"] = pd.to_numeric(output["implied_volatility"], errors="coerce")
    if output["implied_volatility"].isna().any():
        raise ValueError("implied_volatility contains non-numeric values")
    if iv_unit == "percent":
        output["implied_volatility"] = output["implied_volatility"] / 100.0

    source_override = str(source or "").strip()
    if "source" not in output.columns:
        if not source_override:
            raise ValueError("source provenance is required in the input or through --source")
        output["source"] = source_override
    elif source_override:
        output["source"] = output["source"].fillna(source_override).replace("", source_override)

    model_override = str(model or "").strip()
    if "model" not in output.columns and model_override:
        output["model"] = model_override
    elif model_override and "model" in output.columns:
        output["model"] = output["model"].fillna(model_override).replace("", model_override)
    return output


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
