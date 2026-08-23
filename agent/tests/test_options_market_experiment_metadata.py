from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pandas as pd

from src.options_market.databento_metadata import _daily_volume_row
from src.options_market.store import OptionsResearchStore

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "backfill_option_metadata.py"
SPEC = importlib.util.spec_from_file_location("backfill_option_metadata", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

CONTRACT = "AAPL260918C00200000"
UNDERLYING = "AAPL.US"


def test_metadata_fallback_is_point_in_time() -> None:
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
                "source": "test:cbbo",
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
                "value": 1234,
                "source": "test:oi",
                "available_at": "2026-08-24T12:45:01+00:00",
            },
            {
                "contract_symbol": CONTRACT,
                "underlying": UNDERLYING,
                "event_ts": "2026-08-24T20:00:00+00:00",
                "reference_ts": "2026-08-24T04:00:00+00:00",
                "metric": "daily_volume",
                "value": 9876,
                "source": "test:volume",
                "available_at": "2026-08-24T20:00:00+00:00",
            },
        ]
    )

    with OptionsResearchStore(":memory:") as store:
        store.ingest_option_quotes(quote)
        store.ingest_option_statistics(stats)
        intraday = store.option_quotes_with_metadata_asof(
            as_of="2026-08-24T19:45:00+00:00",
            start="2026-08-24T19:00:00+00:00",
            contracts=[CONTRACT],
        )
        post_close = store.option_quotes_with_metadata_asof(
            as_of="2026-08-24T20:30:00+00:00",
            start="2026-08-24T19:00:00+00:00",
            contracts=[CONTRACT],
        )

    assert int(intraday.iloc[0]["open_interest"]) == 1234
    assert pd.isna(intraday.iloc[0]["volume"])
    assert int(post_close.iloc[0]["volume"]) == 9876


def test_daily_volume_uses_early_close_not_normal_close() -> None:
    row = _daily_volume_row(
        {
            "symbol": CONTRACT,
            "ts_event": "2026-11-27T05:00:00Z",
            "volume": 1000,
        },
        source="test:volume",
    )
    assert row is not None
    assert row["available_at"].isoformat() == "2026-11-27T18:00:00+00:00"


def test_metadata_backfill_plan_only_is_network_and_store_free(tmp_path, monkeypatch) -> None:
    underlyings = tmp_path / "underlyings.json"
    underlyings.write_text(json.dumps({"underlyings": ["AAPL.US", "MSFT.US"]}), encoding="utf-8")
    store = tmp_path / "missing.duckdb"
    output = tmp_path / "plan.json"
    monkeypatch.delenv("DATABENTO_API_KEY", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--store", str(store),
            "--underlyings-file", str(underlyings),
            "--start", "2026-08-01T00:00:00+00:00",
            "--end", "2026-09-01T00:00:00+00:00",
            "--plan-only",
            "--output", str(output),
        ],
    )

    assert MODULE.main() == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["underlyings"] == ["AAPL", "MSFT"]
    assert payload["request_schemas"] == ["definition", "statistics", "ohlcv-1d"]
    assert payload["provider_requests_may_incur_charges"] is True
    assert payload["broker_mutation"] is False
    assert payload["historical_iv_derived"] is False
    assert not store.exists()
