from __future__ import annotations

from datetime import datetime, timezone

from src.trading.connectors.alpaca.sdk import AlpacaConfig
from src.trading_platform.models import (
    DataQualitySummary,
    DeskDecision,
    OpportunityCard,
    PlatformEnvironment,
    RiskSummary,
    TradingDeskSnapshot,
)
from src.trading_platform.preflight import run_platform_preflight
from src.trading_platform.store import TradingPlatformStore

UTC = timezone.utc
NOW = datetime(2026, 8, 24, 15, 0, tzinfo=UTC)


def _paper() -> AlpacaConfig:
    return AlpacaConfig(api_key="paper-key", secret_key="paper-secret", profile="paper")


def _publish_ready_snapshot(path) -> None:
    snapshot = TradingDeskSnapshot(
        environment=PlatformEnvironment.PAPER,
        decision=DeskDecision.TRADE_READY_RESEARCH,
        headline="Ready research",
        opportunities=[
            OpportunityCard(
                symbol="TSLA",
                contract_symbol="TSLA260918C00400000",
                decision=DeskDecision.TRADE_READY_RESEARCH,
            )
        ],
        risk=RiskSummary(account_equity_usd=100_000, trading_blocked=False),
        data_quality=DataQualitySummary(healthy=True),
    )
    with TradingPlatformStore(path) as store:
        store.save_snapshot(snapshot)


def test_preflight_can_be_fully_ready_with_passing_runtime(tmp_path, monkeypatch) -> None:
    from src.trading_platform import preflight as module

    store_path = tmp_path / "desk.duckdb"
    _publish_ready_snapshot(store_path)
    monkeypatch.setattr(module, "_calendar_check", lambda: {"ok": True, "version": "4.13.2"})
    monkeypatch.setattr(
        module,
        "_fetch_paper_account",
        lambda *args, **kwargs: {"status": "ACTIVE", "trading_blocked": False, "equity": "100000", "buying_power": "100000"},
    )
    monkeypatch.setattr(
        module,
        "fetch_alpaca_market_clock",
        lambda *args, **kwargs: {"is_open": True, "timestamp": NOW.isoformat(), "next_open": None, "next_close": None},
    )
    monkeypatch.setattr(
        module,
        "fetch_current_option_quote",
        lambda *args, **kwargs: {"bid": 2.69, "ask": 2.72, "age_seconds": 1.0, "feed": "opra"},
    )
    monkeypatch.setenv("DATABENTO_API_KEY", "test-only")
    result = run_platform_preflight(
        store_path=store_path,
        alpaca_config=_paper(),
        execution_input={
            "candidate": {"contract_symbol": "TSLA260918C00400000"},
            "ev_report": {"positive_ev": True},
            "walk_forward_report": {"decision": "WALK_FORWARD_PASS"},
            "risk_report": {"approved": True},
        },
        now=NOW,
    )
    assert result["platform_operational"] is True
    assert result["research_data_ready"] is True
    assert result["historical_replay_ready"] is True
    assert result["paper_runtime_ready"] is True
    assert result["paper_submit_ready_now"] is True
    assert result["operator_actions"] == []


def test_preflight_reports_external_actions_without_exposing_secret(tmp_path, monkeypatch) -> None:
    from src.trading_platform import preflight as module

    store_path = tmp_path / "desk.duckdb"
    _publish_ready_snapshot(store_path)
    monkeypatch.setattr(module, "_calendar_check", lambda: {"ok": True, "version": "4.13.2"})
    monkeypatch.delenv("DATABENTO_API_KEY", raising=False)
    missing = AlpacaConfig(profile="paper")
    result = run_platform_preflight(
        store_path=store_path,
        alpaca_config=missing,
        option_contract="TSLA260918C00400000",
        now=NOW,
    )
    assert result["platform_operational"] is True
    assert result["paper_runtime_ready"] is False
    assert "configure_alpaca_paper_credentials_in_runtime_or_TAP" in result["operator_actions"]
    assert "set_DATABENTO_API_KEY_in_runtime_for_historical_replay" in result["operator_actions"]
    rendered = str(result)
    assert "secret_key" not in rendered
    assert "api_key" not in rendered


def test_market_closed_is_condition_not_operator_setup_action(tmp_path, monkeypatch) -> None:
    from src.trading_platform import preflight as module

    store_path = tmp_path / "desk.duckdb"
    _publish_ready_snapshot(store_path)
    monkeypatch.setattr(module, "_calendar_check", lambda: {"ok": True, "version": "4.13.2"})
    monkeypatch.setattr(
        module,
        "_fetch_paper_account",
        lambda *args, **kwargs: {"status": "ACTIVE", "trading_blocked": False, "equity": "100000", "buying_power": "100000"},
    )
    monkeypatch.setattr(
        module,
        "fetch_alpaca_market_clock",
        lambda *args, **kwargs: {"is_open": False, "timestamp": NOW.isoformat(), "next_open": "2026-08-24T13:30:00Z", "next_close": None},
    )
    monkeypatch.setattr(
        module,
        "fetch_current_option_quote",
        lambda *args, **kwargs: {"bid": 2.69, "ask": 2.72, "age_seconds": 1.0, "feed": "opra"},
    )
    monkeypatch.setenv("DATABENTO_API_KEY", "test-only")
    result = run_platform_preflight(
        store_path=store_path,
        alpaca_config=_paper(),
        execution_input={
            "candidate": {"contract_symbol": "TSLA260918C00400000"},
            "ev_report": {"positive_ev": True},
            "walk_forward_report": {"decision": "WALK_FORWARD_PASS"},
            "risk_report": {"approved": True},
        },
        now=NOW,
    )
    assert result["paper_runtime_ready"] is True
    assert result["paper_submit_ready_now"] is False
    assert "us_options_market_closed" in result["current_conditions"]
    assert "us_options_market_closed" not in result["operator_actions"]
