from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from src.options_market.historical_execution import (
    HistoricalExecutionConfig,
    label_filled_execution_outcome,
    simulate_long_option_entry,
    summarize_historical_execution,
)

UTC = timezone.utc
DECISION = datetime(2026, 8, 24, 15, 0, tzinfo=UTC)
CONTRACT = "AAPL260918C00110000"


def _candidate(**overrides):
    row = {
        "symbol": "AAPL.US",
        "contract_symbol": CONTRACT,
        "research_time": DECISION.isoformat(),
        "entry_ask": 2.0,
        "bid": 1.9,
        "target_profit_pct": 300.0,
        "ranking_score": 80.0,
    }
    row.update(overrides)
    return row


def _quotes(rows):
    return pd.DataFrame(
        [
            {
                "event_ts": timestamp,
                "bid": bid,
                "ask": ask,
            }
            for timestamp, bid, ask in rows
        ]
    )


def test_quote_before_latency_is_ignored_and_later_limit_trigger_fills() -> None:
    quotes = _quotes(
        [
            ("2026-08-24T15:00:00.500000+00:00", 1.80, 1.90),
            ("2026-08-24T15:00:02+00:00", 1.90, 2.00),
        ]
    )
    result = simulate_long_option_entry(
        _candidate(),
        quotes,
        config=HistoricalExecutionConfig(latency_seconds=1.0, max_wait_seconds=10.0),
    )
    assert result["status"] == "FILLED"
    assert result["fill_time"] == "2026-08-24T15:00:02+00:00"
    assert result["fill_price"] == 2.0
    assert result["wait_seconds"] == 2.0


def test_triggered_order_fills_at_limit_not_better_displayed_ask() -> None:
    result = simulate_long_option_entry(
        _candidate(execution_limit_price=2.10),
        _quotes([("2026-08-24T15:00:02+00:00", 1.85, 1.90)]),
        config=HistoricalExecutionConfig(latency_seconds=1.0, max_wait_seconds=10.0),
    )
    assert result["status"] == "FILLED"
    assert result["trigger_ask"] == 1.9
    assert result["fill_price"] == 2.1
    assert result["implementation_shortfall_vs_decision_ask_pct"] == 5.0
    assert result["implementation_shortfall_usd_per_contract"] == 10.0


def test_limit_never_reached_is_unfilled_not_a_trade() -> None:
    result = simulate_long_option_entry(
        _candidate(),
        _quotes(
            [
                ("2026-08-24T15:00:02+00:00", 2.05, 2.20),
                ("2026-08-24T15:00:08+00:00", 2.10, 2.25),
            ]
        ),
        config=HistoricalExecutionConfig(latency_seconds=1.0, max_wait_seconds=10.0),
    )
    assert result["status"] == "UNFILLED"
    assert result["filled"] is False
    assert result["reason"] == "limit_not_reached_before_timeout"


def test_no_quote_inside_wait_window_is_data_unavailable() -> None:
    result = simulate_long_option_entry(
        _candidate(),
        _quotes(
            [
                ("2026-08-24T15:01:00+00:00", 1.90, 2.00),
                ("2026-08-24T15:02:00+00:00", 1.90, 2.00),
            ]
        ),
        config=HistoricalExecutionConfig(latency_seconds=1.0, max_wait_seconds=30.0),
    )
    assert result["status"] == "DATA_UNAVAILABLE"
    assert result["reason"] == "no_quote_after_latency_within_wait_window"
    assert "quote_resolution_coarse_for_wait_window" in result["warnings"]


def test_wide_spread_trigger_is_not_accepted() -> None:
    result = simulate_long_option_entry(
        _candidate(),
        _quotes([("2026-08-24T15:00:02+00:00", 1.00, 2.00)]),
        config=HistoricalExecutionConfig(
            latency_seconds=1.0,
            max_wait_seconds=10.0,
            max_spread_pct=20.0,
        ),
    )
    assert result["status"] == "UNFILLED"
    assert "eligible_quotes_rejected_by_spread_gate" in result["warnings"]


def test_post_fill_outcome_uses_simulated_fill_price() -> None:
    execution = simulate_long_option_entry(
        _candidate(execution_limit_price=2.20),
        _quotes([("2026-08-24T15:00:02+00:00", 1.90, 2.00)]),
        config=HistoricalExecutionConfig(latency_seconds=1.0, max_wait_seconds=10.0),
    )
    outcome = label_filled_execution_outcome(
        _candidate(),
        execution,
        _quotes(
            [
                ("2026-08-24T15:00:02+00:00", 2.00, 2.10),
                ("2026-08-25T15:00:00+00:00", 4.40, 4.50),
                ("2026-08-26T15:00:00+00:00", 8.80, 8.90),
            ]
        ),
    )
    assert outcome is not None
    assert outcome["decision_entry_ask"] == 2.0
    assert outcome["simulated_fill_price"] == 2.2
    assert outcome["touch_2x"] is True
    assert outcome["target_hit"] is True


def test_execution_summary_separates_unfilled_from_missing_data() -> None:
    summary = summarize_historical_execution(
        [
            {"status": "FILLED", "wait_seconds": 2, "implementation_shortfall_vs_decision_ask_pct": 1.0},
            {"status": "UNFILLED"},
            {"status": "DATA_UNAVAILABLE"},
        ]
    )
    assert summary["candidates"] == 3
    assert summary["filled"] == 1
    assert summary["unfilled"] == 1
    assert summary["data_unavailable"] == 1
    assert summary["fill_rate"] == 0.3333
    assert summary["fill_rate_when_observable"] == 0.5
