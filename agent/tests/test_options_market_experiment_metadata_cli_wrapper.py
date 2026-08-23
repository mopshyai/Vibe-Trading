from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

from src.options_market.metadata_replay import run_historical_experiment_with_metadata

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "run_options_historical_experiment_metadata.py"
SPEC = importlib.util.spec_from_file_location("run_options_historical_experiment_metadata", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_wrapper_only_substitutes_metadata_aware_experiment_function() -> None:
    assert MODULE._BASE.run_historical_research_experiment is run_historical_experiment_with_metadata


def test_wrapper_preserves_plan_only_no_store_behavior(tmp_path, monkeypatch) -> None:
    symbols = tmp_path / "symbols.json"
    symbols.write_text(json.dumps({"symbols": ["AAPL.US"]}), encoding="utf-8")
    output = tmp_path / "plan.json"
    missing_store = tmp_path / "missing.duckdb"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--store", str(missing_store),
            "--start", "2026-11-27",
            "--end", "2026-11-27",
            "--evaluation-as-of", "2026-12-31T21:00:00+00:00",
            "--symbols-file", str(symbols),
            "--allow-static-universe",
            "--plan-only",
            "--output", str(output),
        ],
    )
    assert MODULE.main() == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["mode"] == "historical_research_experiment_plan"
    assert payload["provider_network_requests"] is False
    assert payload["broker_mutation"] is False
    assert not missing_store.exists()
