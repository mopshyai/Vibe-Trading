"""Tests for the high-upside options opportunity scanner.

No test reaches Yahoo or a broker. Network access is mocked at the shared
``yahoo_client.get_options`` boundary.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from unittest.mock import patch

import pytest

from src.tools import options_chain_tool as oc
from src.tools._options_opportunity import OpportunityConfig, rank_contract


def _contract(
    *,
    symbol: str = "TEST",
    strike: float = 105.0,
    bid: float = 0.95,
    ask: float = 1.0,
    volume: int = 500,
    open_interest: int = 2_000,
    iv: float = 0.60,
    itm: bool = False,
) -> dict:
    return {
        "contractSymbol": symbol,
        "strike": strike,
        "bid": bid,
        "ask": ask,
        "volume": volume,
        "openInterest": open_interest,
        "impliedVolatility": iv,
        "inTheMoney": itm,
    }


def _rank(contract: dict, *, option_type: str = "call"):
    return rank_contract(
        ticker="TEST",
        option_type=option_type,
        contract=contract,
        spot=100.0,
        expiration=2_000_000_000,
        dte=30,
        config=OpportunityConfig(),
    )


def test_default_300_percent_target_means_four_x_premium() -> None:
    candidate = _rank(_contract(ask=1.0))

    assert candidate is not None
    assert candidate["target_profit_pct"] == 300.0
    assert candidate["target_multiple"] == 4.0
    assert candidate["target_premium"] == 4.0
    assert candidate["max_loss_usd"] == 100.0
    assert candidate["modeled_profit_usd"] == 300.0


def test_zero_dte_cannot_be_enabled_through_default_scanner_config() -> None:
    with pytest.raises(ValueError, match="0DTE"):
        OpportunityConfig.from_kwargs({"min_dte": 0})


def test_wide_spread_is_rejected() -> None:
    candidate = _rank(_contract(bid=0.50, ask=1.0))

    assert candidate is None


def test_low_liquidity_is_rejected() -> None:
    assert _rank(_contract(open_interest=20)) is None
    assert _rank(_contract(volume=2)) is None


def test_more_liquid_equivalent_contract_scores_higher() -> None:
    thin = _rank(
        _contract(
            symbol="THIN",
            open_interest=100,
            volume=20,
        )
    )
    liquid = _rank(
        _contract(
            symbol="LIQUID",
            open_interest=10_000,
            volume=2_000,
        )
    )

    assert thin is not None
    assert liquid is not None
    assert liquid["score"] > thin["score"]


def test_put_with_impossible_four_x_intrinsic_target_is_rejected() -> None:
    candidate = _rank(
        _contract(
            strike=5.0,
            bid=1.90,
            ask=2.0,
        ),
        option_type="put",
    )

    assert candidate is None


def test_chain_mode_remains_backward_compatible() -> None:
    result = {
        "expirationDates": [2_000_000_000],
        "options": [
            {
                "expirationDate": 2_000_000_000,
                "calls": [_contract(symbol="CALL")],
                "puts": [_contract(symbol="PUT", strike=95.0)],
            }
        ],
    }
    with patch.object(oc.yahoo_client, "get_options", return_value=result):
        payload = json.loads(oc.OptionsChainTool().execute(ticker="TEST"))

    assert payload["ok"] is True
    assert "mode" not in payload
    assert payload["data"]["calls_count"] == 1
    assert payload["data"]["puts_count"] == 1


def test_opportunities_mode_scans_and_ranks_without_order_side_effects() -> None:
    expiration = int(
        (datetime.now(timezone.utc) + timedelta(days=30)).timestamp()
    )
    result = {
        "quote": {"regularMarketPrice": 100.0},
        "expirationDates": [expiration],
        "options": [
            {
                "expirationDate": expiration,
                "calls": [
                    _contract(
                        symbol="LIQUID_CALL",
                        open_interest=10_000,
                        volume=2_000,
                    ),
                    _contract(
                        symbol="WIDE_CALL",
                        bid=0.50,
                        ask=1.0,
                    ),
                ],
                "puts": [
                    _contract(
                        symbol="PUT",
                        strike=95.0,
                        open_interest=1_500,
                        volume=300,
                    )
                ],
            }
        ],
    }

    with patch.object(oc.yahoo_client, "get_options", return_value=result) as fetch:
        payload = json.loads(
            oc.OptionsChainTool().execute(
                ticker="TEST",
                mode="opportunities",
            )
        )

    assert payload["ok"] is True
    assert payload["mode"] == "opportunities"
    assert payload["data"]["target_multiple"] == 4.0
    assert payload["data"]["examined_contracts"] == 3
    assert payload["data"]["eligible_count"] == 2
    assert payload["data"]["candidates"][0]["rank"] == 1
    assert {row["contract_symbol"] for row in payload["data"]["candidates"]} == {
        "LIQUID_CALL",
        "PUT",
    }
    assert "guarantees" in payload["warning"]
    fetch.assert_called_once_with("TEST", expiration=None)


def test_invalid_target_profit_is_rejected_before_ranking() -> None:
    payload = json.loads(
        oc.OptionsChainTool().execute(
            ticker="TEST",
            mode="opportunities",
            target_profit_pct=0,
        )
    )

    assert payload["ok"] is False
    assert "target_profit_pct" in payload["error"]
