from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys

import pandas as pd

from src.options_market.historical_execution import HistoricalExecutionConfig
from src.options_market.historical_execution_experiment import apply_execution_model_to_experiment

UTC = timezone.utc
DECISION = datetime(2026, 8, 24, 15, 0, tzinfo=UTC)
EVALUATION = datetime(2026, 9, 10, 20, 0, tzinfo=UTC)
CONTRACT = "AAPL260918C00110000"

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "apply_historical_execution_model.py"
SPEC = importlib.util.spec_from_file_location("apply_historical_execution_model", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class FakeStore:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def option_quotes_asof(self, *, as_of, start=None, end=None, contracts=None, underlyings=None):
        as_of_ts = pd.Timestamp(as_of)
        self.calls.append(
            {
                "as_of": as_of_ts,
                "start": pd.Timestamp(start),
                "end": pd.Timestamp(end),
            }
        )
        if as_of_ts <= pd.Timestamp(DECISION) + pd.Timedelta(minutes=2):
            return pd.DataFrame(
                [
                    {
                        "contract_symbol": CONTRACT,
                        "event_ts": "2026-08-24T15:01:00+00:00",
                        "bid": 1.90,
                        "ask": 2.00,
                    }
                ]
            )
        return pd.DataFrame(
            [
                {
                    "contract_symbol": CONTRACT,
                    "event_ts": "2026-08-25T15:00:00+00:00",
                    "bid": 4.00,
                    "ask": 4.10,
                },
                {
                    "contract_symbol": CONTRACT,
                    "event_ts": "2026-08-28T15:00:00+00:00",
                    "bid": 8.20,
                    "ask": 8.30,
                },
            ]
        )


def _experiment() -> dict:
    return {
        "experiment_id": "exp-test",
        "created_as_of": EVALUATION.isoformat(),
        "experiment_config": {"outcome_horizon_days": 10},
        "replay_config": {"target_profit_pct": 300.0},
        "selected_candidates": [
            {
                "symbol": "AAPL.US",
                "contract_symbol": CONTRACT,
                "research_time": DECISION.isoformat(),
                "entry_ask": 2.00,
                "bid": 1.90,
                "target_profit_pct": 300.0,
                "expiration": "2026-09-18T20:00:00+00:00",
            }
        ],
        "outcomes": [
            {
                "contract_symbol": CONTRACT,
                "research_time": DECISION.isoformat(),
                "evaluation_end": "2026-09-03T15:00:00+00:00",
            }
        ],
    }


def test_entry_path_asof_is_order_timeout_not_later_evaluation() -> None:
    store = FakeStore()
    report = apply_execution_model_to_experiment(
        store,  # type: ignore[arg-type]
        _experiment(),
        config=HistoricalExecutionConfig(
            latency_seconds=1.0,
            max_wait_seconds=120.0,
            max_spread_pct=20.0,
        ),
    )

    assert store.calls[0]["as_of"] == pd.Timestamp("2026-08-24T15:02:00+00:00")
    assert store.calls[0]["end"] == pd.Timestamp("2026-08-24T15:02:00+00:00")
    assert store.calls[1]["as_of"] == pd.Timestamp(EVALUATION)
    assert report["execution_summary"]["filled"] == 1
    assert report["filled_outcome_summary"]["target_hit_rate"] == 1.0
    assert report["candidate_results"][0]["outcome"]["simulated_fill_price"] == 2.0


def test_unfilled_candidate_has_no_strategy_outcome() -> None:
    class UnfilledStore(FakeStore):
        def option_quotes_asof(self, *, as_of, start=None, end=None, contracts=None, underlyings=None):
            self.calls.append({"as_of": pd.Timestamp(as_of), "start": pd.Timestamp(start), "end": pd.Timestamp(end)})
            return pd.DataFrame(
                [
                    {
                        "contract_symbol": CONTRACT,
                        "event_ts": "2026-08-24T15:01:00+00:00",
                        "bid": 2.10,
                        "ask": 2.25,
                    }
                ]
            )

    report = apply_execution_model_to_experiment(
        UnfilledStore(),  # type: ignore[arg-type]
        _experiment(),
        config=HistoricalExecutionConfig(max_wait_seconds=120.0),
    )
    row = report["candidate_results"][0]
    assert row["execution"]["status"] == "UNFILLED"
    assert row["outcome"] is None
    assert report["filled_outcome_summary"]["filled_outcomes"] == 0


def test_plan_only_reads_experiment_but_not_research_store(tmp_path, monkeypatch) -> None:
    experiment = tmp_path / "experiment.json"
    experiment.write_text(json.dumps(_experiment()), encoding="utf-8")
    store = tmp_path / "missing.duckdb"
    output = tmp_path / "plan.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--store", str(store),
            "--experiment-json", str(experiment),
            "--latency-seconds", "1",
            "--max-wait-seconds", "120",
            "--plan-only",
            "--output", str(output),
        ],
    )

    assert MODULE.main() == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["selected_candidate_count"] == 1
    assert payload["provider_network_requests"] is False
    assert payload["broker_mutation"] is False
    assert payload["evaluation_only"] is True
    assert not store.exists()
