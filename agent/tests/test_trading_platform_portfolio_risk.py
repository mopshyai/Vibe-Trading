from __future__ import annotations

import numpy as np
import pandas as pd

from src.trading_platform.portfolio_risk import (
    AccountRiskConfig,
    assess_account_portfolio_risk,
    risk_summary_from_report,
)


def _option(
    symbol: str,
    *,
    risk: float,
    delta: float = 0.5,
    gamma: float = 0.02,
    theta: float = -0.03,
    vega: float = 0.1,
    spot: float = 200.0,
    sector: str = "",
) -> dict:
    return {
        "symbol": symbol,
        "quantity": 1,
        "side": "long",
        "premium_risk_usd": risk,
        "delta": delta,
        "gamma": gamma,
        "theta": theta,
        "vega": vega,
        "underlying_price": spot,
        "sector": sector,
    }


def test_equity_cost_basis_is_not_option_premium_risk() -> None:
    report = assess_account_portfolio_risk(
        [
            {"symbol": "AAPL", "quantity": 100, "cost_basis": 18000, "market_value": 19000},
            _option("MSFT260918C00500000", risk=400.0),
        ],
        account_equity_usd=100_000,
    )
    assert report["approved"] is True
    assert report["position_count"] == 2
    assert report["option_position_count"] == 1
    assert report["open_premium_risk_usd"] == 400.0
    equity = [row for row in report["normalized_positions"] if row["symbol"] == "AAPL"][0]
    assert equity["asset_class"] == "equity"
    assert equity["premium_risk_usd"] is None


def test_same_underlying_concentration_blocks() -> None:
    report = assess_account_portfolio_risk(
        [
            _option("TSLA260918C00400000", risk=900.0),
            _option("TSLA261016C00420000", risk=900.0),
        ],
        account_equity_usd=100_000,
    )
    assert report["trading_blocked"] is True
    assert "underlying_concentration_limit" in report["blocking_reasons"]
    assert report["concentration"]["underlying"][0]["risk_pct"] == 1.8


def test_daily_weekly_and_drawdown_kill_thresholds() -> None:
    report = assess_account_portfolio_risk(
        [],
        account_equity_usd=100_000,
        daily_realized_pnl_usd=-2_500,
        weekly_realized_pnl_usd=-4_500,
        equity_curve=pd.Series([100_000, 105_000, 95_000]),
    )
    assert report["trading_blocked"] is True
    assert "daily_loss_kill_threshold" in report["blocking_reasons"]
    assert "weekly_loss_kill_threshold" in report["blocking_reasons"]
    assert "account_drawdown_kill_threshold" in report["blocking_reasons"]
    assert report["drawdown"]["max_drawdown_pct"] > 9.0


def test_portfolio_greeks_aggregate_calls_and_puts() -> None:
    report = assess_account_portfolio_risk(
        [
            _option("AAPL260918C00200000", risk=300.0, delta=0.5, spot=200.0),
            _option("TSLA260918P00300000", risk=300.0, delta=-0.4, spot=300.0),
        ],
        account_equity_usd=100_000,
    )
    greeks = report["greeks"]
    assert greeks["delta_shares"] == 10.0
    assert greeks["dollar_delta_usd"] == -2000.0
    assert greeks["theta_scaled_per_day"] == -6.0
    assert greeks["coverage"]["delta_shares"] == {"known": 2, "positions": 2}


def test_correlated_cluster_can_block_even_when_individual_names_fit() -> None:
    base = np.linspace(-0.02, 0.02, 60)
    report = assess_account_portfolio_risk(
        [
            _option("AAA260918C00100000", risk=1600.0),
            _option("BBB260918C00100000", risk=1600.0),
        ],
        account_equity_usd=100_000,
        returns_by_underlying={
            "AAA": pd.Series(base),
            "BBB": pd.Series(base * 0.98 + 0.0001),
        },
        config=AccountRiskConfig(
            max_underlying_risk_pct=2.0,
            max_total_premium_risk_pct=5.0,
            max_correlated_cluster_risk_pct=3.0,
        ),
    )
    assert "correlated_cluster_concentration_limit" in report["blocking_reasons"]
    cluster = report["concentration"]["correlation"]["clusters"][0]
    assert cluster["underlyings"] == ["AAA", "BBB"]
    assert cluster["risk_pct"] == 3.2


def test_short_option_exposure_fails_closed() -> None:
    short = _option("NVDA260918C00200000", risk=500.0)
    short["side"] = "short"
    report = assess_account_portfolio_risk(
        [short],
        account_equity_usd=100_000,
    )
    assert report["trading_blocked"] is True
    assert "unsupported_short_option_exposure" in report["blocking_reasons"]


def test_detailed_report_projects_to_trading_desk_summary() -> None:
    report = assess_account_portfolio_risk(
        [_option("AAPL260918C00200000", risk=250.0)],
        account_equity_usd=50_000,
        daily_realized_pnl_usd=125.0,
    )
    summary = risk_summary_from_report(report)
    assert summary.account_equity_usd == 50_000
    assert summary.open_premium_risk_usd == 250.0
    assert summary.open_premium_risk_pct == 0.5
    assert summary.greeks["delta_shares"] == 50.0
