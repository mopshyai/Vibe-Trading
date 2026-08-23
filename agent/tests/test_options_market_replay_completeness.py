from __future__ import annotations

from datetime import datetime, timezone

from src.options_market.replay import ReplayConfig, _score_store_quote

UTC = timezone.utc
AS_OF = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)


def _row(**overrides):
    row = {
        "underlying": "AAPL.US",
        "contract_symbol": "AAPL260220C00110000",
        "option_type": "call",
        "strike": 110.0,
        "expiration": "2026-02-20T21:00:00+00:00",
        "bid": 1.9,
        "ask": 2.0,
        "open_interest": 500,
        "volume": 100,
        "implied_volatility": 0.50,
    }
    row.update(overrides)
    return row


def test_missing_historical_iv_is_explicit_data_warning() -> None:
    candidate, warnings = _score_store_quote(
        _row(implied_volatility=None),
        spot=100.0,
        as_of=AS_OF,
        config=ReplayConfig(),
    )
    assert candidate is None
    assert warnings == ["historical_implied_volatility_missing"]


def test_complete_quote_can_score_without_missing_iv_warning() -> None:
    candidate, warnings = _score_store_quote(
        _row(),
        spot=100.0,
        as_of=AS_OF,
        config=ReplayConfig(),
    )
    assert candidate is not None
    assert "historical_implied_volatility_missing" not in warnings
    assert candidate["implied_volatility"] == 0.5
