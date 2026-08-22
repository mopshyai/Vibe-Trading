#!/usr/bin/env python3
"""Build exact-candidate EV and walk-forward artifacts from labeled history.

This command never fetches provider data and never touches a broker. It consumes
historical outcome labels that already exist on disk, enforces their
``evaluation_as_of`` anti-lookahead boundary, and writes the two mappings used by
the personal decision layer.
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

from src.options_market.evidence_artifacts import build_candidate_evidence_artifacts  # noqa: E402

UTC = timezone.utc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build empirical option evidence artifacts")
    parser.add_argument("--outcomes-json", required=True, type=Path)
    parser.add_argument("--analysis-json", required=True, type=Path)
    parser.add_argument("--as-of", help="Timezone-aware evidence timestamp; defaults to now UTC")
    parser.add_argument("--ev-output", required=True, type=Path)
    parser.add_argument("--walkforward-output", required=True, type=Path)
    parser.add_argument("--summary-output", type=Path)
    parser.add_argument("--score-width", type=float, default=10.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    as_of = _timestamp(args.as_of) if args.as_of else datetime.now(UTC)
    outcomes = _outcomes(args.outcomes_json)
    candidates = _candidates(args.analysis_json)
    report = build_candidate_evidence_artifacts(
        candidates,
        outcomes,
        as_of=as_of,
        score_width=args.score_width,
    )
    _atomic_json(args.ev_output, report["ev_reports"])
    _atomic_json(args.walkforward_output, report["walkforward_reports"])
    if args.summary_output:
        _atomic_json(args.summary_output, report)
    print(
        json.dumps(
            {
                "status": "ok",
                "as_of": report["as_of"],
                "candidate_count": report["candidate_count"],
                "eligible_historical_outcomes": report["eligible_historical_outcomes"],
                "ev_reports": len(report["ev_reports"]),
                "walkforward_reports": len(report["walkforward_reports"]),
            },
            ensure_ascii=False,
        )
    )
    return 0


def _outcomes(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, Mapping):
        payload = payload.get("outcomes")
    if not isinstance(payload, list):
        raise ValueError("outcomes JSON must be a list or an object containing an outcomes list")
    return [dict(row) for row in payload if isinstance(row, Mapping)]


def _candidates(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("analysis JSON must contain an object")
    final = payload.get("final_stage")
    if isinstance(final, Mapping) and isinstance(final.get("candidates"), list):
        return [dict(row) for row in final["candidates"] if isinstance(row, Mapping)]
    state = payload.get("state")
    if isinstance(state, Mapping) and isinstance(state.get("latest_shortlist"), list):
        return [dict(row) for row in state["latest_shortlist"] if isinstance(row, Mapping)]
    personal = payload.get("personal")
    if isinstance(personal, Mapping) and isinstance(personal.get("candidates"), list):
        return [dict(row) for row in personal["candidates"] if isinstance(row, Mapping)]
    return []


def _timestamp(value: object) -> datetime:
    text = str(value or "").strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("--as-of must include a timezone offset")
    return parsed.astimezone(UTC)


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
