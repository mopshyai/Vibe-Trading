#!/usr/bin/env python3
"""Run the existing historical experiment CLI with point-in-time metadata reads.

This wrapper intentionally reuses `run_options_historical_experiment.py` for all
argument parsing, authoritative session scheduling, survivorship controls,
plan-only behavior and output formatting. The only substitution is the experiment
function: historical option quote reads are enriched through metadata that was
knowable at the same as-of timestamp.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent"
if str(AGENT) not in sys.path:
    sys.path.insert(0, str(AGENT))

from src.options_market.metadata_replay import run_historical_experiment_with_metadata  # noqa: E402

BASE_SCRIPT = ROOT / "scripts" / "run_options_historical_experiment.py"
_SPEC = importlib.util.spec_from_file_location("_vibe_base_historical_experiment", BASE_SCRIPT)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover
    raise RuntimeError(f"cannot load base historical experiment script: {BASE_SCRIPT}")
_BASE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _BASE
_SPEC.loader.exec_module(_BASE)

# Keep one CLI contract. The base script's main() resolves this global at call
# time, so only the experiment engine is replaced; plan-only behavior is exactly
# the same and still performs no provider/broker request.
_BASE.run_historical_research_experiment = run_historical_experiment_with_metadata


def main() -> int:
    return int(_BASE.main())


if __name__ == "__main__":
    raise SystemExit(main())
