#!/usr/bin/env python3
"""Build labeled historical option outcomes from the existing PIT research store.

This command is deliberately provider-offline: it makes NO Databento, Alpaca or
web request. The point-in-time DuckDB must already be populated. Research dates
are sampled from the authoritative XNYS calendar; each selection uses only rows
whose ``available_at`` was known then, while labeling is performed separately at
one later ``evaluation_as_of`` timestamp.

Because replay across thousands of symbols and many dates is CPU/data intensive,
this command is explicit rather than part of every continuous worker cycle.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent"
if str(AGENT) not in sys.path:
    sys.path.insert(0, str(AGENT))

from src.options_market.replay import ReplayConfig, replay_many  # noqa: E402
from src.options_market.store import OptionsResearchStore  # noqa: E402
from src.trading_platform.market_calendar import xnys_session_calendar  # noqa: E402

UTC = timezone.utc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build point-in-time historical option outcomes")
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--symbols-file", required=True, type=Path)
    parser.add_argument("--start", required=True, help="First research date, YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="Last calendar date considered, YYYY-MM-DD")
    parser.add_argument(
        "--evaluation-as-of",
        help="Timezone-aware final labeling timestamp; defaults to the final XNYS session close in --end range",
    )
    parser.add_argument("--sample-every-sessions", type=int, default=5)
    parser.add_argument(
        "--label-buffer-days",
        type=int,
        default=70,
        help="Exclude research dates within this many calendar days of evaluation so 7-60 DTE paths can mature",
    )
    parser.add_argument("--max-research-times", type=int, default=300)
    parser.add_argument("--max-symbols", type=int)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.sample_every_sessions < 1:
        raise ValueError("sample-every-sessions must be at least 1")
    if args.label_buffer_days < 1:
        raise ValueError("label-buffer-days must be at least 1")
    if args.max_research_times < 1:
        raise ValueError("max-research-times must be at least 1")

    start = _date(args.start, "start")
    end = _date(args.end, "end")
    if end < start:
        raise ValueError("end must be on or after start")
    symbols = _symbols(args.symbols_file)
    if args.max_symbols is not None:
        if args.max_symbols < 1:
            raise ValueError("max-symbols must be at least 1")
        symbols = symbols[: args.max_symbols]
    if not symbols:
        raise ValueError("symbols file contains no symbols")

    calendar = xnys_session_calendar(start, end)
    sessions = [
        row
        for _, row in sorted(calendar.items())
        if isinstance(row, Mapping) and bool(row.get("trading_day"))
    ]
    if not sessions:
        raise ValueError("calendar range contains no XNYS sessions")

    evaluation = _timestamp(args.evaluation_as_of) if args.evaluation_as_of else _timestamp(sessions[-1]["close"])
    cutoff = evaluation - timedelta(days=args.label_buffer_days)
    research_times = [
        _timestamp(row["close"]) + timedelta(minutes=5)
        for row in sessions
        if _timestamp(row["close"]) + timedelta(minutes=5) <= cutoff
    ]
    research_times = research_times[:: args.sample_every_sessions]
    if len(research_times) > args.max_research_times:
        # Keep the most recent bounded sample so model evidence reflects the
        # current market era while retaining chronological order.
        research_times = research_times[-args.max_research_times :]
    if not research_times:
        raise ValueError("no research sessions remain after the labeling buffer")

    with OptionsResearchStore(args.store) as store:
        replay = replay_many(
            store,
            symbols,
            research_times,
            evaluation_as_of=evaluation,
            config=ReplayConfig(),
        )

    payload = {
        "schema_version": 1,
        "mode": "historical_option_outcomes",
        "generated_at": datetime.now(UTC).isoformat(),
        "store": str(args.store),
        "symbols_requested": len(symbols),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "evaluation_as_of": evaluation.isoformat(),
        "label_buffer_days": args.label_buffer_days,
        "sample_every_sessions": args.sample_every_sessions,
        "research_time_count": len(research_times),
        "research_times": [value.isoformat() for value in research_times],
        "selection_count": replay.get("selection_count"),
        "selected_candidate_count": replay.get("selected_candidate_count"),
        "labeled_outcome_count": replay.get("labeled_outcome_count"),
        "outcomes": replay.get("outcomes") or [],
        "warning": replay.get("warning"),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False, default=str) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": "ok",
                "symbols_requested": len(symbols),
                "research_time_count": len(research_times),
                "selected_candidate_count": payload["selected_candidate_count"],
                "labeled_outcome_count": payload["labeled_outcome_count"],
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 0


def _symbols(path: Path) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, Mapping):
        values = payload.get("symbols")
    else:
        values = payload
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


def _date(value: object, name: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ValueError(f"{name} must be YYYY-MM-DD") from None


def _timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include a timezone offset")
    return parsed.astimezone(UTC)


if __name__ == "__main__":
    raise SystemExit(main())
