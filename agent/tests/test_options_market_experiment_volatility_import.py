from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pandas as pd

from src.options_market.historical_volatility_store import HistoricalOptionVolatilityStore

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "import_historical_option_volatility.py"
SPEC = importlib.util.spec_from_file_location("import_historical_option_volatility", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

CONTRACT = "AAPL260918C00110000"


def _csv(path: Path) -> None:
    pd.DataFrame(
        [
            {
                "contract_symbol": CONTRACT,
                "event_ts": "2026-08-24T15:30:00+00:00",
                "available_at": "2026-08-24T15:31:00+00:00",
                "implied_volatility": 50.0,
                "delta": 0.45,
                "gamma": 0.02,
                "theta": -0.10,
                "vega": 0.12,
            }
        ]
    ).to_csv(path, index=False)


def test_plan_only_converts_units_without_creating_store(tmp_path, monkeypatch) -> None:
    input_path = tmp_path / "vol.csv"
    output = tmp_path / "plan.json"
    store = tmp_path / "volatility.duckdb"
    _csv(input_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--store", str(store),
            "--input", str(input_path),
            "--iv-unit", "percent",
            "--source", "licensed:test",
            "--plan-only",
            "--output", str(output),
        ],
    )

    assert MODULE.main() == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["rows"] == 1
    assert payload["contracts"] == 1
    assert payload["iv_unit_input"] == "percent"
    assert payload["iv_unit_store"] == "fraction"
    assert payload["provider_network_requests"] is False
    assert payload["broker_mutation"] is False
    assert not store.exists()


def test_real_local_import_stores_fraction_iv_and_provenance(tmp_path, monkeypatch) -> None:
    input_path = tmp_path / "vol.csv"
    output = tmp_path / "result.json"
    store_path = tmp_path / "volatility.duckdb"
    _csv(input_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--store", str(store_path),
            "--input", str(input_path),
            "--iv-unit", "percent",
            "--source", "licensed:test",
            "--model", "vendor-model-v1",
            "--output", str(output),
        ],
    )

    assert MODULE.main() == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["inserted"] == 1
    with HistoricalOptionVolatilityStore(store_path) as store:
        rows = store.latest_asof(
            as_of="2026-08-24T16:00:00+00:00",
            contracts=[CONTRACT],
        )
    assert rows.iloc[0]["implied_volatility"] == 0.5
    assert rows.iloc[0]["source"] == "licensed:test"
    assert rows.iloc[0]["model"] == "vendor-model-v1"
