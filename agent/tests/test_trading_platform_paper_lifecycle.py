from __future__ import annotations

from datetime import datetime, timezone

from src.trading.connectors.alpaca.sdk import AlpacaConfig
from src.trading_platform.models import SystemIdentity
from src.trading_platform.paper_lifecycle import (
    PaperLifecycleConfig,
    run_paper_option_lifecycle,
    sync_paper_order_lifecycle,
)
from src.trading_platform.store import TradingPlatformStore

UTC = timezone.utc
NOW = datetime(2026, 8, 24, 15, 0, tzinfo=UTC)


def _candidate() -> dict:
    return {
        "symbol": "TSLA",
        "contract_symbol": "TSLA260918C00400000",
        "option_type": "call",
        "direction": "bullish",
        "target_profit_pct": 300.0,
        "composite_score": 82.0,
        "ranking_score": 84.0,
        "option_quality_score": 80.0,
        "regime_fit_score": 76.0,
        "max_loss_usd_per_contract": 275.0,
    }


def _ev() -> dict:
    return {"positive_ev": True}


def _walk() -> dict:
    return {"decision": "WALK_FORWARD_PASS"}


def _risk() -> dict:
    return {"approved": True}


def _paper() -> AlpacaConfig:
    return AlpacaConfig(api_key="paper-key", secret_key="paper-secret", profile="paper")


def test_dry_run_uses_runtime_clock_and_quote_and_journals_proposal(tmp_path, monkeypatch) -> None:
    from src.trading_platform import paper_lifecycle as module

    monkeypatch.setattr(
        module,
        "fetch_alpaca_market_clock",
        lambda *args, **kwargs: {"is_open": True, "timestamp": NOW.isoformat(), "next_open": None, "next_close": None},
    )
    monkeypatch.setattr(
        module,
        "fetch_current_option_quote",
        lambda *args, **kwargs: {
            "contract_symbol": "TSLA260918C00400000",
            "feed": "opra",
            "execution_grade_feed": True,
            "bid": 2.69,
            "ask": 2.72,
            "quote_time": NOW.isoformat(),
            "age_seconds": 0.0,
        },
    )

    with TradingPlatformStore(tmp_path / "desk.duckdb") as store:
        result = run_paper_option_lifecycle(
            _candidate(),
            ev_report=_ev(),
            walk_forward_report=_walk(),
            risk_report=_risk(),
            alpaca_config=_paper(),
            store=store,
            now=NOW,
        )
        assert result["decision"] == "PAPER_ORDER_DRY_RUN"
        assert result["submitted"] is False
        assert result["quote"]["ask"] == 2.72
        journal = store.recent_journal(limit=10)
        assert journal[0]["stage"] == "proposed"
        assert journal[0]["contract_symbol"] == "TSLA260918C00400000"


def test_actual_paper_submit_requires_opra_before_network(monkeypatch) -> None:
    from src.trading_platform import paper_lifecycle as module

    called = {"clock": False}

    def clock(*args, **kwargs):
        called["clock"] = True
        raise AssertionError("runtime reads should not happen after feed rejection")

    monkeypatch.setattr(module, "fetch_alpaca_market_clock", clock)
    result = run_paper_option_lifecycle(
        _candidate(),
        ev_report=_ev(),
        walk_forward_report=_walk(),
        risk_report=_risk(),
        submit=True,
        confirm_submit=True,
        alpaca_config=_paper(),
        config=PaperLifecycleConfig(option_feed="indicative"),
        now=NOW,
    )
    assert result["decision"] == "PAPER_RUNTIME_REJECTED"
    assert "opra_required_for_paper_submission" in result["reasons"]
    assert called["clock"] is False


def test_live_profile_is_rejected_before_runtime_reads(monkeypatch) -> None:
    from src.trading_platform import paper_lifecycle as module

    monkeypatch.setattr(
        module,
        "fetch_alpaca_market_clock",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not read live profile")),
    )
    live = AlpacaConfig(api_key="live-key", secret_key="live-secret", profile="live")
    result = run_paper_option_lifecycle(
        _candidate(),
        ev_report=_ev(),
        walk_forward_report=_walk(),
        risk_report=_risk(),
        alpaca_config=live,
        now=NOW,
    )
    assert result["decision"] == "PAPER_RUNTIME_REJECTED"
    assert "alpaca_paper_profile_required" in result["reasons"]


def test_stale_option_quote_fails_closed(monkeypatch) -> None:
    from src.trading_platform import paper_lifecycle as module

    monkeypatch.setattr(
        module,
        "fetch_alpaca_market_clock",
        lambda *args, **kwargs: {"is_open": True, "timestamp": NOW.isoformat(), "next_open": None, "next_close": None},
    )
    monkeypatch.setattr(
        module,
        "fetch_current_option_quote",
        lambda *args, **kwargs: {
            "contract_symbol": "TSLA260918C00400000",
            "feed": "opra",
            "execution_grade_feed": True,
            "bid": 2.69,
            "ask": 2.72,
            "quote_time": NOW.isoformat(),
            "age_seconds": 180.0,
        },
    )
    result = run_paper_option_lifecycle(
        _candidate(),
        ev_report=_ev(),
        walk_forward_report=_walk(),
        risk_report=_risk(),
        alpaca_config=_paper(),
        config=PaperLifecycleConfig(max_quote_age_seconds=90.0),
        now=NOW,
    )
    assert result["decision"] == "PAPER_RUNTIME_REJECTED"
    assert "option_quote_stale" in result["reasons"]


def test_submit_then_immediate_fill_is_journaled(tmp_path, monkeypatch) -> None:
    from src.trading_platform import paper_lifecycle as module

    monkeypatch.setattr(
        module,
        "fetch_alpaca_market_clock",
        lambda *args, **kwargs: {"is_open": True, "timestamp": NOW.isoformat(), "next_open": None, "next_close": None},
    )
    monkeypatch.setattr(
        module,
        "fetch_current_option_quote",
        lambda *args, **kwargs: {
            "contract_symbol": "TSLA260918C00400000",
            "feed": "opra",
            "execution_grade_feed": True,
            "bid": 2.69,
            "ask": 2.72,
            "quote_time": NOW.isoformat(),
            "age_seconds": 0.0,
        },
    )
    monkeypatch.setattr(
        module,
        "submit_paper_option_order",
        lambda *args, **kwargs: {
            "ready": True,
            "decision": "PAPER_ORDER_SUBMITTED",
            "submitted": True,
            "reasons": [],
            "order": {"symbol": "TSLA260918C00400000", "quantity": 1, "limit_price": 2.72},
            "broker_result": {"status": "ok", "is_paper": True, "order_id": "paper-123"},
            "max_premium_risk_usd": 272.0,
            "target_premium": 10.88,
        },
    )
    monkeypatch.setattr(
        module,
        "fetch_alpaca_order",
        lambda *args, **kwargs: {
            "order_id": "paper-123",
            "symbol": "TSLA260918C00400000",
            "status": "filled",
            "filled_qty": 1.0,
            "filled_avg_price": 2.71,
        },
    )

    with TradingPlatformStore(tmp_path / "desk.duckdb") as store:
        result = run_paper_option_lifecycle(
            _candidate(),
            ev_report=_ev(),
            walk_forward_report=_walk(),
            risk_report=_risk(),
            submit=True,
            confirm_submit=True,
            alpaca_config=_paper(),
            store=store,
            system=SystemIdentity(),
            now=NOW,
        )
        assert result["submitted"] is True
        rows = store.recent_journal(limit=10)
        assert rows[0]["stage"] == "filled"
        assert rows[0]["fill_price"] == 2.71
        assert rows[1]["stage"] == "submitted"
        assert rows[1]["broker_order_id"] == "paper-123"


def test_sync_is_idempotent_for_same_filled_stage(tmp_path, monkeypatch) -> None:
    from src.trading_platform import paper_lifecycle as module
    from src.trading_platform.journal import JournalEntry, JournalStage
    from src.trading_platform.models import DeskDecision, PlatformEnvironment

    with TradingPlatformStore(tmp_path / "desk.duckdb") as store:
        store.append_journal_entry(
            JournalEntry(
                stage=JournalStage.SUBMITTED,
                environment=PlatformEnvironment.PAPER,
                system=SystemIdentity(),
                symbol="TSLA",
                contract_symbol="TSLA260918C00400000",
                decision=DeskDecision.TRADE_READY_RESEARCH,
                broker_order_id="paper-123",
                quantity=1,
            )
        )
        monkeypatch.setattr(
            module,
            "fetch_alpaca_order",
            lambda *args, **kwargs: {
                "order_id": "paper-123",
                "symbol": "TSLA260918C00400000",
                "status": "filled",
                "filled_qty": 1.0,
                "filled_avg_price": 2.71,
            },
        )
        first = sync_paper_order_lifecycle(store=store, alpaca_config=_paper())
        count_after_first = store.counts()["journal"]
        second = sync_paper_order_lifecycle(store=store, alpaca_config=_paper())
        count_after_second = store.counts()["journal"]
        assert first["synced"][0]["journal_appended"] is True
        assert second["synced"][0]["journal_appended"] is False
        assert count_after_first == count_after_second
