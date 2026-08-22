from __future__ import annotations

from datetime import timezone
import json

import pandas as pd

from src.options_market.databento_adapter import (
    DatabentoHistoricalAdapter,
    DatabentoHistoricalConfig,
    equity_ohlcv_record_to_store_row,
    option_cbbo_record_to_store_row,
    parse_osi_symbol,
)
from src.options_market.store import OptionsResearchStore


def test_parse_osi_symbol() -> None:
    contract = parse_osi_symbol("SPY   241115P00525000")
    assert contract.root == "SPY"
    assert contract.project_underlying == "SPY.US"
    assert contract.option_type == "put"
    assert contract.strike == 525.0
    assert contract.expiration.astimezone(timezone.utc).isoformat() == "2024-11-15T21:00:00+00:00"


def test_equity_daily_bar_becomes_available_at_market_close() -> None:
    row = equity_ohlcv_record_to_store_row(
        {
            "symbol": "AAPL",
            "hd": {"ts_event": "2026-01-05T05:00:00Z"},
            "open": "250.00",
            "high": "255.00",
            "low": "248.00",
            "close": "254.00",
            "volume": 50_000_000,
        },
        source="databento:EQUS.SUMMARY:ohlcv-1d",
    )
    assert row is not None
    assert row["symbol"] == "AAPL.US"
    assert row["available_at"].isoformat() == "2026-01-05T21:00:00+00:00"


def test_option_cbbo_uses_receive_time_and_bid_ask() -> None:
    row = option_cbbo_record_to_store_row(
        {
            "symbol": "AAPL  260220C00270000",
            "ts_recv": "2026-01-05T15:31:00Z",
            "bid_px_00": "2.40",
            "ask_px_00": "2.45",
            "price": "2.43",
        },
        source="databento:OPRA.PILLAR:cbbo-1m",
    )
    assert row is not None
    assert row["underlying"] == "AAPL.US"
    assert row["contract_symbol"] == "AAPL260220C00270000"
    assert row["bid"] == 2.4
    assert row["ask"] == 2.45
    assert row["available_at"] == row["event_ts"]


def test_crossed_cbbo_is_dropped_before_store() -> None:
    row = option_cbbo_record_to_store_row(
        {
            "symbol": "AAPL  260220C00270000",
            "ts_recv": "2026-01-05T15:31:00Z",
            "bid_px_00": "2.50",
            "ask_px_00": "2.45",
        },
        source="fixture",
    )
    assert row is None


class _FakeResponse:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def raise_for_status(self) -> None:
        return None

    def iter_lines(self, decode_unicode: bool = True):  # type: ignore[no-untyped-def]
        del decode_unicode
        for row in self._rows:
            yield json.dumps(row)


class _FakeSession:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.calls: list[dict] = []

    def post(self, url: str, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append({"url": url, **kwargs})
        return _FakeResponse(self.rows)


def test_adapter_streams_equity_records_into_store_without_exposing_key(tmp_path) -> None:
    session = _FakeSession(
        [
            {
                "symbol": "AAPL",
                "hd": {"ts_event": "2026-01-05T05:00:00Z"},
                "open": "250",
                "high": "255",
                "low": "248",
                "close": "254",
                "volume": 50_000_000,
            }
        ]
    )
    adapter = DatabentoHistoricalAdapter(
        api_key="secret-test-key",
        session=session,  # type: ignore[arg-type]
        config=DatabentoHistoricalConfig(ingest_chunk_rows=100),
    )
    with OptionsResearchStore(tmp_path / "research.duckdb") as store:
        count = adapter.ingest_equity_daily_bars(
            store,
            start="2026-01-05",
            end="2026-01-06",
            symbols=["AAPL.US"],
        )
        visible = store.equity_bars_asof(
            ["AAPL.US"],
            start="2026-01-05",
            end="2026-01-06",
            as_of="2026-01-05T22:00:00Z",
        )

    assert count == 1
    assert len(visible) == 1
    assert session.calls[0]["auth"] == ("secret-test-key", "")
    assert "secret-test-key" not in str(session.calls[0]["data"])
    assert session.calls[0]["data"]["dataset"] == "EQUS.SUMMARY"
