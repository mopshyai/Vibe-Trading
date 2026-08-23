from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

import src.options_market.replay as replay
from src.options_market.historical_volatility_store import HistoricalOptionVolatilityStore
from src.options_market.historical_volatility_view import HistoricalVolatilityResearchStoreView

UTC = timezone.utc
CONTRACT = "AAPL260918C00110000"
AS_OF = datetime(2026, 8, 24, 20, 30, tzinfo=UTC)


class QuoteStore:
    def __init__(self, *, native_iv: float | None = None) -> None:
        self.native_iv = native_iv

    def option_quotes_asof(self, *, as_of, start=None, end=None, underlyings=None, contracts=None):
        return pd.DataFrame(
            [
                {
                    "contract_symbol": CONTRACT,
                    "underlying": "AAPL.US",
                    "event_ts": "2026-08-24T15:00:00+00:00",
                    "expiration": "2026-09-18T20:00:00+00:00",
                    "strike": 110.0,
                    "option_type": "call",
                    "bid": 4.8,
                    "ask": 5.0,
                    "open_interest": 1000,
                    "volume": 200,
                    "implied_volatility": self.native_iv,
                    "source": "quote:test",
                    "available_at": "2026-08-24T15:00:01+00:00",
                },
                {
                    "contract_symbol": CONTRACT,
                    "underlying": "AAPL.US",
                    "event_ts": "2026-08-24T16:00:00+00:00",
                    "expiration": "2026-09-18T20:00:00+00:00",
                    "strike": 110.0,
                    "option_type": "call",
                    "bid": 4.9,
                    "ask": 5.0,
                    "open_interest": 1000,
                    "volume": 220,
                    "implied_volatility": self.native_iv,
                    "source": "quote:test",
                    "available_at": "2026-08-24T16:00:01+00:00",
                },
            ]
        )

    def equity_frames_asof(self, symbols, *, start, end, as_of, interval):
        index = pd.date_range("2025-01-02", periods=230, freq="B", tz="UTC")
        close = pd.Series(range(100, 330), index=index, dtype=float)
        return {
            "AAPL.US": pd.DataFrame(
                {
                    "open": close - 1.0,
                    "high": close + 1.0,
                    "low": close - 2.0,
                    "close": close,
                    "volume": 10_000_000.0,
                },
                index=index,
            )
        }

    def option_definitions_asof(self, *args, **kwargs):
        return pd.DataFrame()

    def option_statistics_asof(self, *args, **kwargs):
        return pd.DataFrame()

    def counts(self):
        return {"option_quotes": 2}


def _observations() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "contract_symbol": CONTRACT,
                "event_ts": "2026-08-23T20:00:00+00:00",
                "implied_volatility": 0.40,
                "delta": 0.40,
                "gamma": 0.02,
                "theta": -0.10,
                "vega": 0.12,
                "source": "vendor:eod",
                "model": "vendor_model_v1",
                "available_at": "2026-08-23T20:05:00+00:00",
            },
            {
                "contract_symbol": CONTRACT,
                "event_ts": "2026-08-24T15:30:00+00:00",
                "implied_volatility": 0.50,
                "delta": 0.45,
                "gamma": 0.025,
                "theta": -0.12,
                "vega": 0.13,
                "source": "vendor:intraday",
                "model": "vendor_model_v1",
                "available_at": "2026-08-24T15:31:00+00:00",
            },
            {
                # Same event as the first record, but correction is not knowable
                # until after AS_OF and therefore must stay invisible.
                "contract_symbol": CONTRACT,
                "event_ts": "2026-08-23T20:00:00+00:00",
                "implied_volatility": 0.90,
                "delta": 0.90,
                "source": "vendor:late_revision",
                "available_at": "2026-08-25T12:00:00+00:00",
            },
        ]
    )


def test_volatility_store_hides_later_revision_until_available() -> None:
    with HistoricalOptionVolatilityStore(":memory:") as store:
        store.ingest(_observations())
        rows = store.observations_asof(
            as_of=AS_OF,
            contracts=[CONTRACT],
        )
        first = rows.sort_values("event_ts").iloc[0]
        assert first["implied_volatility"] == 0.40
        assert first["source"] == "vendor:eod"


def test_overlay_uses_prior_event_not_future_event() -> None:
    with HistoricalOptionVolatilityStore(":memory:") as volatility:
        volatility.ingest(_observations())
        view = HistoricalVolatilityResearchStoreView(QuoteStore(native_iv=None), volatility)
        rows = view.option_quotes_asof(as_of=AS_OF, contracts=[CONTRACT])

        assert rows.iloc[0]["implied_volatility"] == 0.40
        assert rows.iloc[0]["delta"] == 0.40
        assert rows.iloc[0]["volatility_source"] == "vendor:eod"
        assert rows.iloc[1]["implied_volatility"] == 0.50
        assert rows.iloc[1]["delta"] == 0.45
        assert rows.iloc[1]["volatility_source"] == "vendor:intraday"


def test_quote_native_iv_wins_over_overlay() -> None:
    with HistoricalOptionVolatilityStore(":memory:") as volatility:
        volatility.ingest(_observations())
        view = HistoricalVolatilityResearchStoreView(QuoteStore(native_iv=0.35), volatility)
        rows = view.option_quotes_asof(as_of=AS_OF, contracts=[CONTRACT])
        assert rows.iloc[0]["implied_volatility"] == 0.35
        assert rows.iloc[1]["implied_volatility"] == 0.35
        # Missing native Greeks may still be filled by the overlay.
        assert rows.iloc[1]["delta"] == 0.45


def test_store_rejects_percent_iv_when_fraction_contract_is_required() -> None:
    bad = _observations().iloc[[0]].copy()
    bad["implied_volatility"] = 40.0
    with HistoricalOptionVolatilityStore(":memory:") as store:
        with pytest.raises(ValueError, match="positive fraction"):
            store.ingest(bad)


def _chart_stage(*args, **kwargs):
    return {
        "candidate_count": 1,
        "candidates": [
            {
                "symbol": "AAPL.US",
                "rank": 1,
                "direction": "bullish",
                "setup_type": "breakout",
                "chart_score": 80.0,
                "realized_vol20_pct": 30.0,
            }
        ],
    }


def test_existing_replay_becomes_iv_complete_only_through_overlay(monkeypatch) -> None:
    monkeypatch.setattr(replay, "scan_market_frames", _chart_stage)
    config = replay.ReplayConfig(
        history_days=220,
        chart_top_n=10,
        max_deep_symbols=5,
        final_top_n=3,
        min_dte=7,
        max_dte=60,
        max_required_move_vs_one_sigma=5.0,
    )

    raw_result = replay.replay_selection_at(
        QuoteStore(native_iv=None),  # type: ignore[arg-type]
        ["AAPL.US"],
        research_time=AS_OF,
        config=config,
    )
    assert "historical_implied_volatility_missing" in raw_result["data_completeness_warnings"]

    with HistoricalOptionVolatilityStore(":memory:") as volatility:
        volatility.ingest(_observations())
        view = HistoricalVolatilityResearchStoreView(QuoteStore(native_iv=None), volatility)
        enriched_result = replay.replay_selection_at(
            view,  # type: ignore[arg-type]
            ["AAPL.US"],
            research_time=AS_OF,
            config=config,
        )

    assert "historical_implied_volatility_missing" not in enriched_result["data_completeness_warnings"]
    assert enriched_result["final_stage"]["candidate_count"] == 1
