from __future__ import annotations

import pandas as pd
import pytest

from src.options_market.store import OptionsResearchStore


def _equity_row(*, close: float, available_at: str) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "symbol": "ABC.US",
                "event_ts": "2026-01-02T21:00:00Z",
                "interval": "1D",
                "open": 99.0,
                "high": max(101.0, close),
                "low": min(98.0, close),
                "close": close,
                "volume": 2_000_000,
                "source": "fixture",
                "available_at": available_at,
            }
        ]
    )


def _option_row(*, bid: float, ask: float, event_ts: str, available_at: str) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "contract_symbol": "ABC260220C00105000",
                "underlying": "ABC.US",
                "event_ts": event_ts,
                "expiration": "2026-02-20T21:00:00Z",
                "strike": 105.0,
                "option_type": "call",
                "bid": bid,
                "ask": ask,
                "last": (bid + ask) / 2,
                "volume": 250,
                "open_interest": 2_000,
                "implied_volatility": 0.55,
                "source": "fixture",
                "available_at": available_at,
            }
        ]
    )


def test_equity_asof_query_cannot_see_later_revision(tmp_path) -> None:
    with OptionsResearchStore(tmp_path / "research.duckdb") as store:
        store.ingest_equity_bars(_equity_row(close=100.0, available_at="2026-01-02T21:00:01Z"))
        store.ingest_equity_bars(_equity_row(close=100.5, available_at="2026-01-03T12:00:00Z"))

        before_revision = store.equity_bars_asof(
            ["ABC.US"],
            start="2026-01-02T00:00:00Z",
            end="2026-01-02T23:59:59Z",
            as_of="2026-01-02T22:00:00Z",
        )
        after_revision = store.equity_bars_asof(
            ["ABC.US"],
            start="2026-01-02T00:00:00Z",
            end="2026-01-02T23:59:59Z",
            as_of="2026-01-03T13:00:00Z",
        )

    assert before_revision.iloc[0]["close"] == 100.0
    assert after_revision.iloc[0]["close"] == 100.5


def test_future_option_quote_is_invisible_and_latest_known_revision_wins(tmp_path) -> None:
    with OptionsResearchStore(tmp_path / "research.duckdb") as store:
        store.ingest_option_quotes(
            _option_row(
                bid=1.00,
                ask=1.10,
                event_ts="2026-01-02T15:00:00Z",
                available_at="2026-01-02T15:00:01Z",
            )
        )
        store.ingest_option_quotes(
            _option_row(
                bid=1.02,
                ask=1.12,
                event_ts="2026-01-02T15:00:00Z",
                available_at="2026-01-02T15:01:00Z",
            )
        )
        store.ingest_option_quotes(
            _option_row(
                bid=1.30,
                ask=1.40,
                event_ts="2026-01-02T15:05:00Z",
                available_at="2026-01-02T15:05:01Z",
            )
        )

        rows = store.option_quotes_asof(
            as_of="2026-01-02T15:02:00Z",
            start="2026-01-02T14:59:00Z",
            end="2026-01-02T15:10:00Z",
            underlyings=["ABC.US"],
        )

    assert len(rows) == 1
    assert rows.iloc[0]["bid"] == 1.02
    assert rows.iloc[0]["ask"] == 1.12
    assert rows.iloc[0]["event_ts"] == pd.Timestamp("2026-01-02T15:00:00")


def test_available_at_before_market_event_is_rejected(tmp_path) -> None:
    with OptionsResearchStore(tmp_path / "research.duckdb") as store:
        bad = _equity_row(close=100.0, available_at="2026-01-02T20:59:59Z")
        with pytest.raises(ValueError, match="available_at"):
            store.ingest_equity_bars(bad)


def test_crossed_option_market_is_rejected(tmp_path) -> None:
    with OptionsResearchStore(tmp_path / "research.duckdb") as store:
        bad = _option_row(
            bid=1.20,
            ask=1.10,
            event_ts="2026-01-02T15:00:00Z",
            available_at="2026-01-02T15:00:01Z",
        )
        with pytest.raises(ValueError, match="ask cannot be below bid"):
            store.ingest_option_quotes(bad)


def test_equity_frames_are_scanner_ready(tmp_path) -> None:
    with OptionsResearchStore(tmp_path / "research.duckdb") as store:
        store.ingest_equity_bars(_equity_row(close=100.0, available_at="2026-01-02T21:00:01Z"))
        frames = store.equity_frames_asof(
            ["ABC.US"],
            start="2026-01-02T00:00:00Z",
            end="2026-01-02T23:59:59Z",
            as_of="2026-01-02T22:00:00Z",
        )

    assert list(frames) == ["ABC.US"]
    assert list(frames["ABC.US"].columns) == ["open", "high", "low", "close", "volume"]
    assert frames["ABC.US"].iloc[0]["close"] == 100.0
