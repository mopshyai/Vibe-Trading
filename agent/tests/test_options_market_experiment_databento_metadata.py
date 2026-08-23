from __future__ import annotations

from datetime import datetime, timezone

from src.options_market.databento_adapter import DatabentoHistoricalConfig
from src.options_market.databento_metadata import DatabentoOptionMetadataAdapter
from src.options_market.store import OptionsResearchStore

UTC = timezone.utc
CONTRACT = "AAPL260918C00200000"


class FakeAdapter(DatabentoOptionMetadataAdapter):
    def __init__(self) -> None:
        self.config = DatabentoHistoricalConfig(chunk_size=10)
        self.requests: list[dict] = []

    def _stream_jsonl(self, params):  # type: ignore[override]
        self.requests.append(dict(params))
        schema = str(params.get("schema"))
        if schema == "definition":
            yield {
                "symbol": CONTRACT,
                "ts_recv": "2026-08-24T12:00:00Z",
                "activation": "2026-01-02T14:30:00Z",
                "min_price_increment": 10_000_000,
                "original_contract_size": 100,
            }
        elif schema == "statistics":
            yield {
                "symbol": CONTRACT,
                "stat_type": 9,
                "quantity": 1234,
                "ts_ref": "2026-08-23T20:00:00Z",
                "ts_recv": "2026-08-24T12:45:00Z",
            }
        elif schema == "ohlcv-1d":
            yield {
                "symbol": CONTRACT,
                "ts_event": "2026-08-24T04:00:00Z",
                "volume": 9876,
            }


def test_fake_metadata_adapter_builds_only_expected_opra_requests() -> None:
    adapter = FakeAdapter()
    start = datetime(2026, 8, 24, 0, 0, tzinfo=UTC)
    end = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)
    with OptionsResearchStore(":memory:") as store:
        assert adapter.ingest_option_definitions("AAPL.US", start=start, end=end, store=store) == 1
        assert adapter.ingest_option_open_interest("AAPL.US", start=start, end=end, store=store) == 1
        assert adapter.ingest_option_daily_volume("AAPL.US", start=start, end=end, store=store) == 1
        counts = store.counts()
        assert counts["option_definitions"] == 1
        assert counts["option_statistics"] == 2

    assert [row["schema"] for row in adapter.requests] == ["definition", "statistics", "ohlcv-1d"]
    assert all(row["dataset"] == "OPRA.PILLAR" for row in adapter.requests)
    assert all(row["symbols"] == "AAPL.OPT" for row in adapter.requests)
    assert all(row["stype_in"] == "parent" for row in adapter.requests)
    assert all("api_key" not in row for row in adapter.requests)
