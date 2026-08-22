from __future__ import annotations

import numpy as np
import pandas as pd

from src.options_market.chart_screen import ChartScreenConfig, compute_chart_snapshot, scan_market_frames


def _frame(*, start: float, end: float, volume: float = 2_000_000.0, spike: float = 2.0) -> pd.DataFrame:
    bars = 240
    close = np.linspace(start, end, bars) + np.sin(np.arange(bars) / 7.0) * 0.25
    open_ = close * (1.0 + np.cos(np.arange(bars)) * 0.001)
    high = np.maximum(open_, close) * 1.01
    low = np.minimum(open_, close) * 0.99
    volumes = np.full(bars, volume, dtype=float)
    volumes[-1] *= spike
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volumes},
        index=pd.date_range("2025-01-02", periods=bars, freq="B"),
    )


def test_low_liquidity_is_rejected_before_cross_section() -> None:
    config = ChartScreenConfig(min_avg_dollar_volume=20_000_000)
    assert compute_chart_snapshot("TINY.US", _frame(start=10, end=12, volume=10_000), config) is None


def test_cross_section_maps_uptrend_to_call_and_downtrend_to_put() -> None:
    result = scan_market_frames(
        {
            "BULL.US": _frame(start=50, end=100),
            "BEAR.US": _frame(start=100, end=50),
            "ILLIQUID.US": _frame(start=20, end=25, volume=1_000),
        },
        ChartScreenConfig(min_chart_score=0, min_direction_gap=0, top_n=10),
    )
    by_symbol = {row["symbol"]: row for row in result["candidates"]}
    assert by_symbol["BULL.US"]["direction"] == "bullish"
    assert by_symbol["BULL.US"]["option_type"] == "call"
    assert by_symbol["BEAR.US"]["direction"] == "bearish"
    assert by_symbol["BEAR.US"]["option_type"] == "put"
    assert "ILLIQUID.US" not in by_symbol
    assert result["eligible_for_cross_section"] == 2


def test_scores_are_explicitly_not_probabilities() -> None:
    result = scan_market_frames(
        {"BULL.US": _frame(start=50, end=100), "BEAR.US": _frame(start=100, end=50)},
        ChartScreenConfig(min_chart_score=0, min_direction_gap=0),
    )
    assert result["candidates"]
    assert "not a probability" in result["candidates"][0]["score_interpretation"]
