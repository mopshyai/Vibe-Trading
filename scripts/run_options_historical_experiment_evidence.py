#!/usr/bin/env python3
"""Run the existing historical experiment with metadata + IV/Greeks evidence.

All base experiment arguments remain unchanged. This wrapper adds only
`--volatility-store` and substitutes the composed historical-evidence experiment
function. `--plan-only` still opens neither the research store nor the volatility
store and performs no provider/broker request.
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent"
if str(AGENT) not in sys.path:
    sys.path.insert(0, str(AGENT))

from src.options_market.historical_evidence_replay import (  # noqa: E402
    run_historical_experiment_with_historical_evidence,
)
from src.options_market.historical_volatility_store import HistoricalOptionVolatilityStore  # noqa: E402

BASE_SCRIPT = ROOT / "scripts" / "run_options_historical_experiment.py"
_SPEC = importlib.util.spec_from_file_location("_vibe_base_historical_evidence_experiment", BASE_SCRIPT)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover
    raise RuntimeError(f"cannot load base historical experiment script: {BASE_SCRIPT}")
_BASE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _BASE
_SPEC.loader.exec_module(_BASE)


def _extract_wrapper_args(argv: list[str]) -> tuple[Path | None, list[str]]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--volatility-store", type=Path)
    known, remaining = parser.parse_known_args(argv[1:])
    if known.volatility_store is None and "--help" not in argv and "-h" not in argv:
        raise ValueError("--volatility-store is required")
    return known.volatility_store, [argv[0], *remaining]


def main() -> int:
    volatility_path, base_argv = _extract_wrapper_args(list(sys.argv))
    original_argv = list(sys.argv)
    sys.argv = base_argv
    try:
        def run_with_evidence(store, research_times, **kwargs: Any):
            assert volatility_path is not None
            if not volatility_path.exists():
                raise FileNotFoundError(f"historical volatility store does not exist: {volatility_path}")
            with HistoricalOptionVolatilityStore(volatility_path) as volatility_store:
                return run_historical_experiment_with_historical_evidence(
                    store,
                    volatility_store,
                    research_times,
                    **kwargs,
                )

        _BASE.run_historical_research_experiment = run_with_evidence
        return int(_BASE.main())
    finally:
        sys.argv = original_argv


if __name__ == "__main__":
    raise SystemExit(main())
