from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from src.options_market.databento_adapter import DatabentoHistoricalConfig
from src.options_market.databento_metadata import (
    DatabentoOptionMetadataAdapter,
    _daily_volume_row,
    _definition_row,
    _open_interest_row,
)
from src.options_market.store import OptionsResearchStore

UTC = timezone.utc
CONTRACT = "AAPL260918C00200000"


class FakeMetadataAdapter(DatabentoOptionMetadataAdapter):
    """Exercise request construction without any provider network call."""

    def __init__(self, records_by_schema: dict[str, list[dict]]) -> None:
        self.config = DatabentoHistoricalConfig(chunk_size=2)
        self.records_by_schema = records_by_schema
        self.requests: list[dict] = []

    def _stream_jsonl(self, params):  # type: ignore[override]
        self.requests.append(dict(params))
        yield from self.records_by_schema.get(str(params.get("schema")), [])


def test_definition_row_normalizes_osi_and_fixed_price_tick() -> None:
    row = _definition_row(
        {
            "symbol": CONTRACT,
            "ts_recv": "2026-08-24T12:00:00Z",
            "activation": "2026-01-02T14:30:00Z",
            "min_price_increment": 10_000_000,
            "original_contract_size": 100,
            "security_update_action": "A",
        },
        source="test:definition",
    )
    assert row is not None
    assert row["contract_symbol"] == CONTRACT
    assert row["underlying"] == "AAPL.US"
    assert row["strike"] == 200.0
    assert row["option_type"] == "call"
    assert row["min_price_increment"] == 0.01
    assert row["contract_multiplier"] == 100.0
    assert row["available_at"].isoformat() == "2026-08-24T12:00:00+00:00"


def test_open_interest_accepts_stat_type_9_and_ignores_other_stats() -> None:
    good = _open_interest_row(
        {
            "symbol": CONTRACT,
            "stat_type": 9,
            "quantity": 1234,
            "ts_ref": "2026-08-23T20:00:00Z",
            "ts_recv": "2026-08-24T12:45:00Z",
        },
        source="test:oi",
    )
    ignored = _open_interest_row(
        {
            "symbol": CONTRACT,
            "stat_type": 3,
            "quantity": 99,
            "ts_recv": "2026-08-24T12:45:00Z",
        },
        source="test:oi",
    )
    assert good is not None
    assert good["metric"] == "open_interest"
    assert good["value"] == 1234.0
    assert good["available_at"].isoformat() == "2026-08-24T12:45:00+00:00"
    assert ignored is None


def test_daily_volume_uses_authoritative_post_thanksgiving_early_close() -> None:
    row = _daily_volume_row(
        {
            "symbol": CONTRACT,
            "ts_event": "2026-11-27T05:00:00Z",
            "volume": 9876,
        },
        source="test:volume",
    )
    assert row is not None
    # Friday after Thanksgiving 2026 closes 13:00 ET = 18:00 UTC.
    assert row["event_ts"].isoformat() == "2026-11-27T18:00:00+00:00"
    assert row["available_at"].isoformat() == "2026-11-27T18:00:00+00:00"
    assert row["reference_ts"].isoformat() == "2026-11-27T05:00:00+00:00"
    assert row["value"] == 9876.0


def test_fake_adapter_requests_metadata_schemas_and_writes_store() -> None:
    adapter = FakeMetadataAdapter(
        {
            "definition": [
                {
                    "symbol": CONTRACT,
                    "ts_recv": "2026-08-24T12:00:00Z",
                    "activation": "2026-01-02T14:30:00Z",
                    "min_price_increment": 10_000_000,
                    "original_contract_size": 100,
                }
            ],
            "statistics": [
                {
                    "symbol": CONTRACT,
                    "stat_type": 9,
                    "quantity": 1234,
                    "ts_ref": "2026-08-23T20:00:00Z",
                    "ts_recv": "2026-08-24T12:45:00Z",
                },
                {
                    "symbol": CONTRACT,
                    "stat_type": 3,
                    "quantity": 55,
                    "ts_recv": "2026-08-24T12:45:01Z",
                },
            ],
            "ohlcv-1d": [
                {
                    "symbol": CONTRACT,
                    "ts_event": "2026-08-24T04:00:00Z",
                    "volume": 9876,
                }
            ],
        }
    )
    start = datetime(2026, 8, 24, 0, 0, tzinfo=UTC)
    end = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)

    with OptionsResearchStore(":memory:") as store:
        assert adapter.ingest_option_definitions("AAPL.US", start=start, end=end, store=store) == 1
        assert adapter.ingest_option_open_interest("AAPL.US", start=start, end=end, store=store) == 1
        assert adapter.ingest_option_daily_volume("AAPL.US", start=start, end=end, store=store) == 1

        definitions = store.option_definitions_asof(
            as_of="2026-08-24T19:00:00+00:00",
            contracts=[CONTRACT],
        )
        oi = store.option_statistics_asof(
            as_of="2026-08-24T19:00:00+00:00",
            contracts=[CONTRACT],
            metrics=["open_interest"],
        )
        volume = store.option_statistics_asof(
            as_of="2026-08-24T20:30:00+00:00",
            contracts=[CONTRACT],
            metrics=["daily_volume"],
        )
        assert len(definitions) == 1
        assert int(oi.iloc[0]["value"]) == 1234
        assert int(volume.iloc[0]["value"]) == 9876

    assert [request["schema"] for request in adapter.requests] == [
        "definition",
        "statistics",
        "ohlcv-1d",
    ]
    assert all(request["dataset"] == "OPRA.PILLAR" for request in adapter.requests)
    assert all(request["symbols"] == "AAPL.OPT" for request in adapter.requests)
    assert all(request["stype_in"] == "parent" for request in adapter.requests)
    assert all("api_key" not in request for request in adapter.requests)
