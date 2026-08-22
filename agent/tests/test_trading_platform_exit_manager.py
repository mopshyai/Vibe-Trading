from __future__ import annotations

from datetime import datetime, timezone

from src.trading_platform import (
    ExitPolicyConfig,
    TradingPlatformStore,
    evaluate_long_option_exit,
    scan_long_option_exits,
)

UTC = timezone.utc
NOW = datetime(2026, 8, 24, 15, 0, tzinfo=UTC)
CONTRACT = "AAPL260918C00250000"


def _position(**overrides):
    row = {
        "contract_symbol": CONTRACT,
        "quantity": 1,
        "side": "long",
        "fill_price": 2.0,
        "filled_at": "2026-08-20T15:00:00+00:00",
        "high_watermark_bid": 2.0,
        "thesis_valid": True,
    }
    row.update(overrides)
    return row


def _quote(bid: float, *, seconds_old: int = 10):
    ts = datetime.fromtimestamp(NOW.timestamp() - seconds_old, tz=UTC)
    return {"bid": bid, "ask": bid + 0.05, "quote_time": ts.isoformat()}


def test_profit_target_proposes_exit_at_current_bid() -> None:
    result = evaluate_long_option_exit(_position(), _quote(4.1), now=NOW)
    assert result["decision"] == "EXIT_PROPOSED"
    assert "profit_take_target" in result["exit_reasons"]
    assert result["return_pct"] == 105.0
    assert result["market_value_usd"] == 410.0


def test_hard_stop_accepts_zero_bid_as_real_mark() -> None:
    result = evaluate_long_option_exit(_position(), _quote(0.0), now=NOW)
    assert result["decision"] == "EXIT_PROPOSED"
    assert "hard_stop_loss" in result["exit_reasons"]
    assert result["current_bid"] == 0.0
    assert result["return_pct"] == -100.0


def test_stale_quote_fails_closed_without_exit_instruction() -> None:
    result = evaluate_long_option_exit(_position(), _quote(1.0, seconds_old=180), now=NOW)
    assert result["decision"] == "NO_ACTION"
    assert result["exit_proposed"] is False
    assert "stale_option_quote" in result["blocking_reasons"]


def test_trailing_profit_protection_uses_high_watermark() -> None:
    result = evaluate_long_option_exit(
        _position(high_watermark_bid=4.0),
        _quote(2.9),
        now=NOW,
        config=ExitPolicyConfig(profit_take_pct=150.0),
    )
    assert result["decision"] == "EXIT_PROPOSED"
    assert "trailing_profit_protection" in result["exit_reasons"]
    assert result["peak_gain_pct"] == 100.0
    assert result["drawdown_from_peak_pct"] == 27.5


def test_thesis_invalidation_proposes_exit_before_price_target() -> None:
    result = evaluate_long_option_exit(
        _position(thesis_valid=False),
        _quote(2.1),
        now=NOW,
    )
    assert result["decision"] == "EXIT_PROPOSED"
    assert result["exit_reasons"] == ["thesis_invalidated"]


def test_near_expiry_time_stop_proposes_exit() -> None:
    position = _position(contract_symbol="AAPL260826C00250000")
    result = evaluate_long_option_exit(position, {**_quote(2.05), "contract_symbol": position["contract_symbol"]}, now=NOW)
    assert result["decision"] == "EXIT_PROPOSED"
    assert "time_stop_dte" in result["exit_reasons"]
    assert result["dte"] == 2


def test_scan_journals_exit_proposal_without_broker_mutation(tmp_path) -> None:
    with TradingPlatformStore(tmp_path / "platform.duckdb") as store:
        report = scan_long_option_exits(
            [_position()],
            {CONTRACT: _quote(4.2)},
            now=NOW,
            store=store,
            snapshot_id="snapshot-1",
        )
        assert report["exit_proposals"] == 1
        assert report["broker_mutation"] is False
        journal = store.recent_journal(limit=10)
        assert journal[0]["stage"] == "exit_proposed"
        assert journal[0]["contract_symbol"] == CONTRACT
        assert journal[0]["metadata"]["broker_mutation"] is False
