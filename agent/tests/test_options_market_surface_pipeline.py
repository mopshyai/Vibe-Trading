from __future__ import annotations

from src.options_market.pipeline import combine_rankings


def test_pipeline_preserves_greeks_feed_and_surface_evidence() -> None:
    chart = [
        {
            "symbol": "XYZ",
            "chart_score": 80.0,
            "direction": "bullish",
            "rank": 1,
            "setup_type": "breakout",
            "realized_vol20_pct": 30.0,
        }
    ]
    option = {
        "contract_symbol": "XYZ260918C00110000",
        "option_type": "call",
        "score": 80.0,
        "target_profit_pct": 300.0,
        "required_move_vs_one_sigma": 1.1,
        "required_underlying_move_pct": 20.0,
        "spot": 100.0,
        "strike": 110.0,
        "expiration": "2026-09-18",
        "dte": 25,
        "bid": 1.9,
        "entry_ask": 2.0,
        "spread_pct": 5.13,
        "open_interest": 1200,
        "volume": 300,
        "implied_volatility": 0.50,
        "delta": 0.25,
        "gamma": 0.02,
        "theta": -0.05,
        "vega": 0.10,
        "surface_efficiency_score": 62.0,
        "surface_iv_percentile": 72.0,
        "surface_required_move_ratio": 1.25,
        "surface_context": {
            "surface_efficiency_score": 62.0,
            "surface_iv_percentile": 72.0,
            "required_move_vs_surface_expected_move": 1.25,
            "term_structure_state": "flat",
            "skew_state": "put_skew",
        },
        "data_source": "alpaca",
        "option_feed": "opra",
        "execution_grade_feed": True,
    }

    result = combine_rankings(chart, {"XYZ": [option]})
    row = result["candidates"][0]
    assert row["delta"] == 0.25
    assert row["theta"] == -0.05
    assert row["surface_efficiency_score"] == 62.0
    assert row["surface_context"]["skew_state"] == "put_skew"
    assert row["option_feed"] == "opra"
    assert row["execution_grade_feed"] is True
    assert row["realized_vol20_pct"] == 30.0
