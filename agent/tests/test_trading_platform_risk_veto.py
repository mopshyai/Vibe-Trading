from src.trading_platform import RiskSummary, TradingPlatformService, TradingPlatformStore


def test_account_risk_veto_forces_no_trade_without_erasing_research(tmp_path) -> None:
    cycle = {
        "market": {"state": {"cycle": 17}, "plan": {"phase": "regular"}},
        "personal": {"decision": "TRADE_READY_RESEARCH"},
        "dashboard": {
            "decision": "TRADE_READY_RESEARCH",
            "headline": "One research candidate cleared candidate-level gates",
            "funnel": {
                "universe": 5000,
                "chart_eligible": 1800,
                "chart_candidates": 200,
                "deep_analyzed": 50,
                "positive_ev": 4,
                "risk_approved": 1,
                "trade_ready": 1,
            },
            "cards": [
                {
                    "symbol": "AAPL",
                    "contract_symbol": "AAPL260918C00250000",
                    "decision": "TRADE_READY_RESEARCH",
                    "direction": "bullish",
                    "composite_score": 83.0,
                    "hard_reasons": [],
                    "watch_reasons": [],
                }
            ],
        },
    }
    risk = RiskSummary(
        account_equity_usd=10_000,
        open_premium_risk_usd=700,
        open_premium_risk_pct=7.0,
        positions=3,
        trading_blocked=True,
        blocking_reasons=["total_open_premium_risk_limit"],
    )

    with TradingPlatformStore(tmp_path / "platform.duckdb") as store:
        snapshot = TradingPlatformService(store=store).publish_personal_cycle(cycle, risk=risk)

        assert snapshot.decision.value == "NO_TRADE"
        assert snapshot.funnel.trade_ready == 0
        assert snapshot.risk.trading_blocked is True
        assert snapshot.headline.startswith("NO TRADE")
        assert len(snapshot.opportunities) == 1
        assert snapshot.opportunities[0].decision.value == "TRADE_READY_RESEARCH"

        latest = store.latest_snapshot()
        assert latest is not None
        assert latest.decision.value == "NO_TRADE"
        assert latest.opportunities[0].symbol == "AAPL"
