from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

import src.options_market.replay as replay
from src.options_market.metadata_view import MetadataAwareResearchStoreView

UTC = timezone.utc
AS_OF = datetime(2026, 8, 24, 20, 30, tzinfo=UTC)
CONTRACT = "AAPL260918C00110000"


class FakeMetadataStore:
    def __init__(self, *, iv: float | None) -> None:
        self.iv = iv
        self.quote_reads = 0

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

    def option_quotes_with_metadata_asof(self, *, as_of, start=None, end=None, underlyings=None, contracts=None):
        self.quote_reads += 1
        return pd.DataFrame(
            [
                {
                    "contract_symbol": CONTRACT,
                    "underlying": "AAPL.US",
                    "event_ts": "2026-08-24T20:00:00+00:00",
                    "expiration": "2026-09-18T20:00:00+00:00",
                    "strike": 110.0,
                    "option_type": "call",
                    "bid": 4.9,
                    "ask": 5.0,
                    "volume": 500,
                    "open_interest": 1500,
                    "implied_volatility": self.iv,
                    "source": "metadata-aware:test",
                    "available_at": "2026-08-24T20:00:01+00:00",
                }
            ]
        )


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


def test_metadata_view_feeds_existing_replay_without_missing_liquidity_warnings(monkeypatch) -> None:
    base = FakeMetadataStore(iv=0.50)
    view = MetadataAwareResearchStoreView(base)  # type: ignore[arg-type]
    monkeypatch.setattr(replay, "scan_market_frames", _chart_stage)

    result = replay.replay_selection_at(
        view,  # type: ignore[arg-type]
        ["AAPL.US"],
        research_time=AS_OF,
        config=replay.ReplayConfig(
            history_days=220,
            chart_top_n=10,
            max_deep_symbols=5,
            final_top_n=3,
            min_dte=7,
            max_dte=60,
            max_required_move_vs_one_sigma=5.0,
        ),
    )

    assert base.quote_reads == 1
    assert "historical_open_interest_missing" not in result["data_completeness_warnings"]
    assert "historical_option_volume_missing" not in result["data_completeness_warnings"]
    assert "historical_implied_volatility_missing" not in result["data_completeness_warnings"]
    assert result["final_stage"]["candidate_count"] == 1


def test_metadata_view_does_not_invent_missing_historical_iv(monkeypatch) -> None:
    base = FakeMetadataStore(iv=None)
    view = MetadataAwareResearchStoreView(base)  # type: ignore[arg-type]
    monkeypatch.setattr(replay, "scan_market_frames", _chart_stage)

    result = replay.replay_selection_at(
        view,  # type: ignore[arg-type]
        ["AAPL.US"],
        research_time=AS_OF,
        config=replay.ReplayConfig(
            history_days=220,
            chart_top_n=10,
            max_deep_symbols=5,
            final_top_n=3,
            min_dte=7,
            max_dte=60,
            max_required_move_vs_one_sigma=5.0,
        ),
    )

    assert "historical_implied_volatility_missing" in result["data_completeness_warnings"]
    assert result["final_stage"]["candidate_count"] == 0
