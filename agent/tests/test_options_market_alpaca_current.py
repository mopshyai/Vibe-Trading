from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.options_market.alpaca_current import AlpacaCurrentOptionsConfig, _rank_contract


def test_rank_current_alpaca_contract_preserves_feed_quality_inputs() -> None:
    now = datetime(2026, 8, 24, 14, 0, tzinfo=timezone.utc)
    expiry = (now.date() + timedelta(days=28)).isoformat()
    contract = {
        "symbol": "ABC260921C00110000",
        "strike_price": "110",
        "expiration_date": expiry,
        "open_interest": "5000",
        "tradable": True,
    }
    snapshot = {
        "latestQuote": {"bp": 2.40, "ap": 2.50},
        "impliedVolatility": 0.45,
        "greeks": {"delta": 0.39, "gamma": 0.03, "theta": -0.05, "vega": 0.11},
    }
    cfg = AlpacaCurrentOptionsConfig(option_feed="indicative", max_required_move_vs_one_sigma=2.0)
    result = _rank_contract(
        underlying="ABC",
        contract=contract,
        snapshot=snapshot,
        spot=100.0,
        option_type="call",
        target_profit_pct=300.0,
        now=now,
        config=cfg,
    )
    assert result is not None
    assert result["target_multiple"] == 4.0
    assert result["execution_grade_feed"] is False
    assert result["delta"] == 0.39
    assert result["open_interest"] == 5000
    assert result["volume"] is None


def test_rank_current_alpaca_contract_rejects_wide_market() -> None:
    now = datetime(2026, 8, 24, 14, 0, tzinfo=timezone.utc)
    contract = {
        "symbol": "ABC260921C00110000",
        "strike_price": "110",
        "expiration_date": (now.date() + timedelta(days=28)).isoformat(),
        "open_interest": "5000",
        "tradable": True,
    }
    snapshot = {
        "latestQuote": {"bp": 1.0, "ap": 2.0},
        "impliedVolatility": 0.45,
        "greeks": {"delta": 0.39},
    }
    result = _rank_contract(
        underlying="ABC",
        contract=contract,
        snapshot=snapshot,
        spot=100.0,
        option_type="call",
        target_profit_pct=300.0,
        now=now,
        config=AlpacaCurrentOptionsConfig(max_spread_pct=15.0),
    )
    assert result is None
