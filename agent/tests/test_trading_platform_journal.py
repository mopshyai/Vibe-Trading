from __future__ import annotations

from src.trading_platform import (
    DeskDecision,
    JournalEntry,
    JournalStage,
    PlatformEnvironment,
    TradingPlatformService,
    TradingPlatformStore,
)


def _cycle() -> dict:
    return {
        "market": {
            "plan": {"phase": "regular"},
            "state": {"cycle": 22},
        },
        "personal": {
            "decision": "WATCH",
            "journal_candidates": [
                {
                    "symbol": "AAA",
                    "contract_symbol": "AAA260918C00100000",
                    "decision": "WATCH",
                    "displayed": True,
                    "direction": "bullish",
                    "composite_score": 80.0,
                    "ranking_score": 82.0,
                    "watch_reasons": ["empirical_ev_sample_insufficient"],
                    "ev_samples": 18,
                    "entry_ask": 1.25,
                    "max_loss_usd_per_contract": 125.0,
                    "option_feed": "opra",
                    "execution_grade_feed": True,
                },
                {
                    "symbol": "BBB",
                    "contract_symbol": "BBB260918P00050000",
                    "decision": "PASS",
                    "displayed": False,
                    "direction": "bearish",
                    "composite_score": 53.0,
                    "ranking_score": 60.0,
                    "hard_reasons": ["option_quality_failed"],
                    "entry_ask": 0.85,
                    "max_loss_usd_per_contract": 85.0,
                    "option_feed": "opra",
                    "execution_grade_feed": True,
                },
            ],
        },
        "dashboard": {
            "headline": "No trade-ready setup; strongest candidates remain on watch",
            "decision": "WATCH",
            "market_regime": "mixed",
            "funnel": {"universe": 5000, "final_candidates": 2},
            "cards": [],
        },
        "alert": {"should_alert": False},
        "execution": "none",
    }


def test_service_journals_displayed_and_rejected_candidates(tmp_path) -> None:
    with TradingPlatformStore(tmp_path / "desk.duckdb") as store:
        service = TradingPlatformService(store=store)
        snapshot = service.publish_personal_cycle(_cycle())

        rows = store.recent_journal(10)
        assert len(rows) == 2
        by_symbol = {row["symbol"]: row for row in rows}
        assert by_symbol["AAA"]["stage"] == "watch"
        assert by_symbol["AAA"]["displayed"] is True
        assert by_symbol["BBB"]["stage"] == "rejected"
        assert by_symbol["BBB"]["displayed"] is False
        assert by_symbol["BBB"]["snapshot_id"] == snapshot.snapshot_id
        assert store.counts()["journal"] == 2


def test_journal_filters_by_symbol_and_contract(tmp_path) -> None:
    with TradingPlatformStore(tmp_path / "desk.duckdb") as store:
        for symbol, contract in (
            ("AAA", "AAA260918C00100000"),
            ("BBB", "BBB260918P00050000"),
        ):
            store.append_journal_entry(
                JournalEntry(
                    stage=JournalStage.EVALUATED,
                    environment=PlatformEnvironment.RESEARCH,
                    system={
                        "platform_version": "test",
                        "strategy_version": "test",
                        "model_version": "test",
                        "risk_policy_version": "test",
                        "payoff_policy_version": "test",
                    },
                    symbol=symbol,
                    contract_symbol=contract,
                    decision=DeskDecision.WATCH,
                )
            )

        assert [row["symbol"] for row in store.recent_journal(symbol="AAA")] == ["AAA"]
        assert [row["symbol"] for row in store.recent_journal(contract_symbol="BBB260918P00050000")] == ["BBB"]
