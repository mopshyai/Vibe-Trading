#!/usr/bin/env python3
"""Run a reproducible point-in-time options research experiment.

The command reads only the local OptionsResearchStore. It never downloads market
data and never calls a broker. Use --plan-only to inspect the session schedule and
universe mode before running a potentially large local replay.
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

from src.options_market import (  # noqa: E402
    ExperimentLineage,
    HistoricalExperimentConfig,
    OptionsResearchStore,
    ReplayConfig,
    run_historical_research_experiment,
)
from src.trading_platform import xnys_session_calendar  # noqa: E402

UTC = timezone.utc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run point-in-time historical options research")
    parser.add_argument("--store", required=True, type=Path, help="Existing OptionsResearchStore DuckDB path")
    parser.add_argument("--start", required=True, help="First calendar date, YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="Last calendar date, YYYY-MM-DD")
    parser.add_argument("--evaluation-as-of", required=True, help="Timezone-aware later timestamp used for outcome labels")
    universe = parser.add_mutually_exclusive_group(required=True)
    universe.add_argument("--universe-snapshots-json", type=Path, help="Point-in-time universe snapshots")
    universe.add_argument("--symbols-file", type=Path, help="Static symbols; requires --allow-static-universe")
    parser.add_argument("--allow-static-universe", action="store_true")
    parser.add_argument("--cadence-sessions", type=int, default=1, help="Replay every Nth XNYS session")
    parser.add_argument("--minutes-before-close", type=int, default=30)
    parser.add_argument("--outcome-horizon-days", type=int, default=30)
    parser.add_argument("--min-outcome-observations", type=int, default=2)
    parser.add_argument("--min-bucket-samples", type=int, default=30)
    parser.add_argument("--max-sessions", type=int, default=1000)
    parser.add_argument("--history-days", type=int, default=420)
    parser.add_argument("--chart-top-n", type=int, default=200)
    parser.add_argument("--max-deep-symbols", type=int, default=50)
    parser.add_argument("--final-top-n", type=int, default=10)
    parser.add_argument("--min-dte", type=int, default=7)
    parser.add_argument("--max-dte", type=int, default=60)
    parser.add_argument("--target-profit-pct", type=float, default=300.0)
    parser.add_argument("--quote-lookback-minutes", type=int, default=30)
    parser.add_argument("--commit-sha")
    parser.add_argument("--data-snapshot-id", help="Immutable identifier for the local research dataset/backfill")
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.cadence_sessions < 1:
        raise ValueError("cadence-sessions must be at least 1")
    if args.minutes_before_close < 1:
        raise ValueError("minutes-before-close must be positive")

    evaluation = _aware(args.evaluation_as_of, "evaluation-as-of")
    research_times = _research_schedule(
        args.start,
        args.end,
        cadence_sessions=args.cadence_sessions,
        minutes_before_close=args.minutes_before_close,
    )
    if not research_times:
        raise ValueError("date range contains no XNYS trading sessions")

    static_symbols = None
    snapshots = None
    if args.symbols_file:
        static_symbols = _symbols_file(args.symbols_file)
        if not args.allow_static_universe:
            raise ValueError(
                "--symbols-file is survivorship-biased unless --allow-static-universe is explicitly supplied"
            )
    else:
        snapshots = _universe_snapshots(args.universe_snapshots_json)

    replay_cfg = ReplayConfig(
        history_days=args.history_days,
        chart_top_n=args.chart_top_n,
        max_deep_symbols=args.max_deep_symbols,
        final_top_n=args.final_top_n,
        min_dte=args.min_dte,
        max_dte=args.max_dte,
        target_profit_pct=args.target_profit_pct,
        quote_lookback_minutes=args.quote_lookback_minutes,
    )
    experiment_cfg = HistoricalExperimentConfig(
        outcome_horizon_days=args.outcome_horizon_days,
        minimum_quote_observations=args.min_outcome_observations,
        max_sessions=args.max_sessions,
        allow_static_universe=args.allow_static_universe,
        min_bucket_samples=args.min_bucket_samples,
    )
    lineage = ExperimentLineage(
        commit_sha=(
            str(args.commit_sha or os.environ.get("RENDER_GIT_COMMIT") or os.environ.get("GIT_COMMIT_SHA") or "").strip()
            or None
        ),
        data_snapshot_id=str(args.data_snapshot_id or "").strip() or None,
    )

    plan = {
        "mode": "historical_research_experiment_plan",
        "store": str(args.store),
        "start": args.start,
        "end": args.end,
        "evaluation_as_of": evaluation.isoformat(),
        "research_times": [value.isoformat() for value in research_times],
        "session_count": len(research_times),
        "cadence_sessions": args.cadence_sessions,
        "minutes_before_close": args.minutes_before_close,
        "universe_mode": "static_explicitly_allowed" if static_symbols is not None else "point_in_time_snapshots",
        "static_symbol_count": None if static_symbols is None else len(static_symbols),
        "universe_snapshot_count": None if snapshots is None else len(snapshots),
        "replay_config": replay_cfg.__dict__,
        "experiment_config": experiment_cfg.__dict__,
        "lineage": lineage.__dict__,
        "provider_network_requests": False,
        "broker_mutation": False,
    }
    if args.plan_only:
        return _emit(plan, args.output)

    if len(research_times) > args.max_sessions:
        raise ValueError(f"planned sessions exceed max-sessions={args.max_sessions}")
    if not args.store.exists():
        raise FileNotFoundError(f"research store does not exist: {args.store}")

    with OptionsResearchStore(args.store) as store:
        report = run_historical_research_experiment(
            store,
            research_times,
            evaluation_as_of=evaluation,
            static_symbols=static_symbols,
            universe_snapshots=snapshots,
            replay_config=replay_cfg,
            experiment_config=experiment_cfg,
            lineage=lineage,
        )
    report["plan"] = plan
    return _emit(report, args.output)


def _research_schedule(
    start: str,
    end: str,
    *,
    cadence_sessions: int,
    minutes_before_close: int,
) -> list[datetime]:
    calendar = xnys_session_calendar(start, end)
    sessions: list[datetime] = []
    for day in sorted(calendar):
        row = calendar[day]
        if not bool(row.get("trading_day")):
            continue
        close = _aware(str(row.get("close")), f"calendar close {day}")
        sessions.append(close - timedelta(minutes=minutes_before_close))
    return sessions[::cadence_sessions]


def _universe_snapshots(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("snapshots") if isinstance(payload, Mapping) else payload
    if not isinstance(rows, list):
        raise ValueError("universe snapshot JSON must be a list or {snapshots:[...]}")
    output: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError(f"universe snapshot {index} must be an object")
        symbols = row.get("symbols")
        if not isinstance(symbols, list) or not symbols:
            raise ValueError(f"universe snapshot {index} requires a non-empty symbols list")
        output.append(
            {
                "available_at": row.get("available_at"),
                "symbols": [str(item).strip().upper() for item in symbols if str(item).strip()],
                "source": row.get("source"),
            }
        )
    return output


def _symbols_file(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, Mapping):
        values = payload.get("symbols")
    elif isinstance(payload, list):
        values = payload
    else:
        values = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not isinstance(values, list):
        raise ValueError("symbols file must be a JSON list, {symbols:[...]}, or one symbol per line")
    symbols: list[str] = []
    seen: set[str] = set()
    for value in values:
        symbol = str(value or "").strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            symbols.append(symbol)
    if not symbols:
        raise ValueError("symbols file contains no symbols")
    return symbols


def _aware(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{name} must be an ISO timezone-aware timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _emit(payload: Mapping[str, Any], output: Path | None) -> int:
    rendered = json.dumps(dict(payload), indent=2, ensure_ascii=False, allow_nan=False, default=str) + "\n"
    if output is None:
        print(rendered, end="")
        return 0
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=str(output.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, output)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
