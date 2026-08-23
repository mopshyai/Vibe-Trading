from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pandas as pd
import pytest

from src.options_market.historical_evidence_replay import historical_evidence_store_view
from src.options_market.historical_volatility_store import HistoricalOptionVolatilityStore
from src.options_market.store import OptionsResearchStore

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "run_options_historical_experiment_evidence.py"
SPEC = importlib.util.spec_from_file_location("run_options_historical_experiment_evidence", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

CONTRACT = "AAPL260918C00200000"
UNDERLYING = "AAPL.US"


def test_composed_view_fills_liquidity_then_iv_and_greeks() -> None:
    quote = pd.DataFrame(
        [
            {
                "contract_symbol": CONTRACT,
                "underlying": UNDERLYING,
                "event_ts": "2026-08-24T19:30:00+00:00",
                "expiration": "2026-09-18T20:00:00+00:00",
                "strike": 200.0,
                "option_type": "call",
                "bid": 4.9,
                "ask": 5.0,
                "volume": None,
                "open_interest": None,
                "implied_volatility": None,
                "source": "opra:cbbo",
                "available_at": "2026-08-24T19:30:01+00:00",
            }
        ]
    )
    stats = pd.DataFrame(
        [
            {
                "contract_symbol": CONTRACT,
                "underlying": UNDERLYING,
                "event_ts": "2026-08-24T12:45:00+00:00",
                "reference_ts": "2026-08-23T20:00:00+00:00",
                "metric": "open_interest",
                "value": 1500,
                "source": "opra:oi",
                "available_at": "2026-08-24T12:45:01+00:00",
            },
            {
                "contract_symbol": CONTRACT,
                "underlying": UNDERLYING,
                "event_ts": "2026-08-23T20:00:00+00:00",
                "reference_ts": "2026-08-23T04:00:00+00:00",
                "metric": "daily_volume",
                "value": 600,
                "source": "opra:volume",
                "available_at": "2026-08-23T20:00:00+00:00",
            },
        ]
    )
    volatility = pd.DataFrame(
        [
            {
                "contract_symbol": CONTRACT,
                "event_ts": "2026-08-23T20:00:00+00:00",
                "implied_volatility": 0.42,
                "delta": 0.45,
                "gamma": 0.02,
                "theta": -0.11,
                "vega": 0.13,
                "source": "licensed:vendor",
                "model": "vendor-v1",
                "available_at": "2026-08-23T20:05:00+00:00",
            }
        ]
    )

    with OptionsResearchStore(":memory:") as research, HistoricalOptionVolatilityStore(":memory:") as vol_store:
        research.ingest_option_quotes(quote)
        research.ingest_option_statistics(stats)
        vol_store.ingest(volatility)
        view = historical_evidence_store_view(research, vol_store)
        rows = view.option_quotes_asof(
            as_of="2026-08-24T19:45:00+00:00",
            start="2026-08-24T19:00:00+00:00",
            contracts=[CONTRACT],
        )

    row = rows.iloc[0]
    assert int(row["open_interest"]) == 1500
    assert int(row["volume"]) == 600
    assert row["implied_volatility"] == 0.42
    assert row["delta"] == 0.45
    assert row["volatility_source"] == "licensed:vendor"
    assert row["volatility_model"] == "vendor-v1"


def test_full_evidence_cli_plan_only_opens_neither_store(tmp_path, monkeypatch) -> None:
    symbols = tmp_path / "symbols.json"
    symbols.write_text(json.dumps({"symbols": ["AAPL.US"]}), encoding="utf-8")
    research_store = tmp_path / "missing-research.duckdb"
    volatility_store = tmp_path / "missing-volatility.duckdb"
    output = tmp_path / "plan.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--volatility-store", str(volatility_store),
            "--store", str(research_store),
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
    assert payload["provider_network_requests"] is False
    assert payload["broker_mutation"] is False
    assert not research_store.exists()
    assert not volatility_store.exists()


def test_full_evidence_cli_fails_closed_when_volatility_store_missing(tmp_path, monkeypatch) -> None:
    symbols = tmp_path / "symbols.json"
    symbols.write_text(json.dumps({"symbols": ["AAPL.US"]}), encoding="utf-8")
    research_store = tmp_path / "research.duckdb"
    volatility_store = tmp_path / "missing-volatility.duckdb"
    with OptionsResearchStore(research_store):
        pass

    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--volatility-store", str(volatility_store),
            "--store", str(research_store),
            "--start", "2026-11-27",
            "--end", "2026-11-27",
            "--evaluation-as-of", "2026-12-31T21:00:00+00:00",
            "--symbols-file", str(symbols),
            "--allow-static-universe",
            "--max-sessions", "10",
        ],
    )

    with pytest.raises(FileNotFoundError, match="volatility store does not exist"):
        MODULE.main()
    assert not volatility_store.exists()
