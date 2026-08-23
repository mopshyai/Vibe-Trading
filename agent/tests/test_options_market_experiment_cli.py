from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "run_options_historical_experiment.py"
SPEC = importlib.util.spec_from_file_location("run_options_historical_experiment", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_post_thanksgiving_schedule_respects_early_close() -> None:
    schedule = MODULE._research_schedule(
        "2026-11-27",
        "2026-11-27",
        cadence_sessions=1,
        minutes_before_close=30,
    )
    assert len(schedule) == 1
    # 2026-11-27 XNYS closes 13:00 ET / 18:00 UTC; research is 30m before.
    assert schedule[0].isoformat() == "2026-11-27T17:30:00+00:00"


def test_schedule_rejects_research_time_before_session_open() -> None:
    with pytest.raises(ValueError, match="before XNYS open"):
        MODULE._research_schedule(
            "2026-11-27",
            "2026-11-27",
            cadence_sessions=1,
            minutes_before_close=220,
        )


def test_plan_only_needs_no_existing_store_or_provider_request(tmp_path, monkeypatch) -> None:
    symbols = tmp_path / "symbols.json"
    symbols.write_text(json.dumps({"symbols": ["AAPL.US", "MSFT.US"]}), encoding="utf-8")
    output = tmp_path / "plan.json"
    missing_store = tmp_path / "does-not-exist.duckdb"
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
            "--data-snapshot-id", "test-dataset-1",
            "--plan-only",
            "--output", str(output),
        ],
    )
    assert MODULE.main() == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["session_count"] == 1
    assert payload["universe_mode"] == "static_explicitly_allowed"
    assert payload["provider_network_requests"] is False
    assert payload["broker_mutation"] is False
    assert payload["lineage"]["data_snapshot_id"] == "test-dataset-1"
    assert not missing_store.exists()


def test_cli_requires_static_universe_acknowledgement(tmp_path, monkeypatch) -> None:
    symbols = tmp_path / "symbols.txt"
    symbols.write_text("AAPL.US\n", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--store", str(tmp_path / "missing.duckdb"),
            "--start", "2026-11-27",
            "--end", "2026-11-27",
            "--evaluation-as-of", "2026-12-31T21:00:00+00:00",
            "--symbols-file", str(symbols),
            "--plan-only",
        ],
    )
    with pytest.raises(ValueError, match="survivorship-biased"):
        MODULE.main()
