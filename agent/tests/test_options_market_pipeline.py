from __future__ import annotations

from src.options_market.pipeline import OptionsMarketPipelineConfig, combine_rankings


def _chart(symbol: str, direction: str, score: float) -> dict:
    return {
        "symbol": symbol,
        "direction": direction,
        "setup_type": "breakout",
        "rank": 1,
        "chart_score": score,
    }


def _option(option_type: str, *, score: float = 80, move_ratio: float = 1.0) -> dict:
    return {
        "option_type": option_type,
        "target_profit_pct": 300.0,
        "score": score,
        "required_move_vs_one_sigma": move_ratio,
        "contract_symbol": "XYZ",
        "entry_ask": 1.0,
        "max_loss_usd": 100.0,
        "target_premium": 4.0,
        "required_underlying_move_pct": 8.0,
        "spread_pct": 5.0,
        "open_interest": 1000,
        "volume": 250,
        "implied_volatility": 0.55,
        "dte": 30,
    }


def test_direction_must_match_option_side() -> None:
    result = combine_rankings(
        [_chart("ABC.US", "bullish", 85)],
        {"ABC.US": [_option("put"), _option("call")]},
    )
    assert result["candidate_count"] == 1
    assert result["candidates"][0]["option_type"] == "call"


def test_implausible_required_move_is_rejected() -> None:
    result = combine_rankings(
        [_chart("ABC.US", "bullish", 85)],
        {"ABC.US": [_option("call", move_ratio=2.5)]},
        config=OptionsMarketPipelineConfig(max_required_move_vs_one_sigma=1.75),
    )
    assert result["decision"] == "NO_TRADE"
    assert result["candidates"] == []


def test_catalyst_score_changes_ranking_without_becoming_probability() -> None:
    result = combine_rankings(
        [_chart("AAA.US", "bullish", 80), _chart("BBB.US", "bullish", 80)],
        {"AAA.US": [_option("call", score=80)], "BBB.US": [_option("call", score=80)]},
        catalyst_scores={"AAA.US": 90, "BBB.US": 20},
    )
    assert result["candidates"][0]["symbol"] == "AAA.US"
    assert "not probability" in result["candidates"][0]["score_interpretation"]


def test_300_percent_target_is_four_times_premium() -> None:
    result = combine_rankings(
        [_chart("ABC.US", "bullish", 85)],
        {"ABC.US": [_option("call")]},
    )
    assert result["target_multiple"] == 4.0


def test_invalid_direction_fails_closed() -> None:
    result = combine_rankings(
        [_chart("ABC.US", "sideways", 85)],
        {"ABC.US": [_option("put")]},
    )
    assert result["decision"] == "NO_TRADE"
    assert result["rejected"][0]["reason"] == "invalid_chart_direction"


def test_malformed_target_profit_does_not_crash() -> None:
    option = _option("call")
    option["target_profit_pct"] = None
    result = combine_rankings(
        [_chart("ABC.US", "bullish", 85)],
        {"ABC.US": [option]},
    )
    assert result["candidate_count"] == 1
