from __future__ import annotations

import pandas as pd

from src.options_market.outcomes import OutcomeConfig, calibrate_score_buckets, label_long_option_path


def _candidate(entry: float = 1.0, score: float = 80.0) -> dict:
    return {
        "symbol": "ABC.US",
        "contract_symbol": "ABC-C",
        "option_type": "call",
        "entry_ask": entry,
        "ranking_score": score,
        "chart_score": 78,
        "option_score": 82,
    }


def test_300_percent_target_labels_four_x_touch_using_bid() -> None:
    quotes = pd.DataFrame(
        {"bid": [0.90, 1.80, 3.90, 4.05, 3.70], "ask": [1.10, 2.00, 4.20, 4.30, 4.00]},
        index=pd.date_range("2026-01-01", periods=5, freq="h"),
    )
    result = label_long_option_path(_candidate(), quotes)
    assert result["target_multiple"] == 4.0
    assert result["target_premium"] == 4.0
    assert result["target_hit"] is True
    assert result["touch_4x"] is True
    assert result["max_multiple"] == 4.05
    assert result["exit_mark"] == "bid"


def test_ask_only_touch_does_not_count_as_exit_target() -> None:
    quotes = pd.DataFrame({"bid": [0.9, 3.95], "ask": [1.1, 4.2]})
    result = label_long_option_path(_candidate(), quotes)
    assert result["target_hit"] is False


def test_full_loss_proxy_uses_configured_drawdown() -> None:
    quotes = pd.DataFrame({"bid": [0.8, 0.04, 0.1]})
    result = label_long_option_path(_candidate(), quotes, config=OutcomeConfig(full_loss_threshold_pct=95))
    assert result["full_loss_proxy"] is True
    assert result["mae_pct"] == -96.0


def test_score_bucket_calibration_reports_sample_validity() -> None:
    outcomes = []
    for i in range(4):
        outcomes.append(
            {
                "ranking_score": 82 + i,
                "target_hit": i < 2,
                "touch_2x": i < 3,
                "touch_3x": i < 2,
                "touch_4x": i < 2,
                "full_loss_proxy": i == 3,
                "max_multiple": [4.5, 4.1, 2.4, 0.8][i],
                "end_return_pct": [250, 180, 40, -70][i],
            }
        )
    result = calibrate_score_buckets(outcomes, config=OutcomeConfig(min_bucket_samples=5))
    bucket = result["buckets"][0]
    assert bucket["samples"] == 4
    assert bucket["empirical_target_hit_rate"] == 0.5
    assert bucket["calibration_valid"] is False
    assert "not guaranteed future probabilities" in result["warning"]
