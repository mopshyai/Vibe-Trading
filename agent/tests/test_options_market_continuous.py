from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.options_market.continuous import (
    AtomicAnalysisStateStore,
    ContinuousAnalysisState,
    ContinuousScanConfig,
    MarketPhase,
    TradingSession,
    classify_market_phase,
    plan_analysis_cycle,
    weekday_regular_session,
)

UTC = timezone.utc


def _session() -> TradingSession:
    return TradingSession(
        open_at=datetime(2026, 8, 24, 13, 30, tzinfo=UTC),
        close_at=datetime(2026, 8, 24, 20, 0, tzinfo=UTC),
        source="test-calendar",
        authoritative=True,
    )


def test_market_phase_tracks_us_regular_session_windows() -> None:
    session = _session()
    assert classify_market_phase(datetime(2026, 8, 24, 12, 0, tzinfo=UTC), session) is MarketPhase.PREMARKET
    assert classify_market_phase(datetime(2026, 8, 24, 13, 35, tzinfo=UTC), session) is MarketPhase.OPENING
    assert classify_market_phase(datetime(2026, 8, 24, 16, 0, tzinfo=UTC), session) is MarketPhase.REGULAR
    assert classify_market_phase(datetime(2026, 8, 24, 19, 15, tzinfo=UTC), session) is MarketPhase.POWER_HOUR
    assert classify_market_phase(datetime(2026, 8, 24, 20, 30, tzinfo=UTC), session) is MarketPhase.POSTMARKET
    assert classify_market_phase(datetime(2026, 8, 25, 1, 0, tzinfo=UTC), session) is MarketPhase.CLOSED


def test_non_trading_day_is_closed_even_during_normal_hours() -> None:
    session = TradingSession(
        open_at=datetime(2026, 8, 22, 13, 30, tzinfo=UTC),
        close_at=datetime(2026, 8, 22, 20, 0, tzinfo=UTC),
        trading_day=False,
    )
    assert classify_market_phase(datetime(2026, 8, 22, 16, 0, tzinfo=UTC), session) is MarketPhase.CLOSED


def test_first_regular_cycle_bootstraps_full_and_focus_work() -> None:
    now = datetime(2026, 8, 24, 16, 0, tzinfo=UTC)
    plan = plan_analysis_cycle(now, _session(), ContinuousAnalysisState())
    assert plan.phase is MarketPhase.REGULAR
    assert plan.full_scan is True
    assert plan.focus_refresh is True
    assert "regular_full_scan_due" in plan.reasons


def test_focus_refresh_is_faster_than_full_scan() -> None:
    now = datetime(2026, 8, 24, 16, 0, tzinfo=UTC)
    state = ContinuousAnalysisState(
        last_full_scan_at=(now - timedelta(minutes=10)).isoformat(),
        last_focus_scan_at=(now - timedelta(minutes=6)).isoformat(),
        chart_candidates=[{"symbol": "AAPL.US", "chart_score": 80, "direction": "bullish"}],
    )
    plan = plan_analysis_cycle(now, _session(), state)
    assert plan.full_scan is False
    assert plan.focus_refresh is True
    assert "regular_focus_refresh_due" in plan.reasons


def test_opening_phase_rechecks_focus_every_two_minutes() -> None:
    now = datetime(2026, 8, 24, 13, 40, tzinfo=UTC)
    state = ContinuousAnalysisState(
        last_full_scan_at=(now - timedelta(minutes=5)).isoformat(),
        last_focus_scan_at=(now - timedelta(minutes=3)).isoformat(),
        chart_candidates=[{"symbol": "TSLA.US", "chart_score": 82, "direction": "bullish"}],
    )
    plan = plan_analysis_cycle(now, _session(), state)
    assert plan.phase is MarketPhase.OPENING
    assert plan.full_scan is False
    assert plan.focus_refresh is True


def test_closed_market_does_not_poll_options_unless_forced() -> None:
    now = datetime(2026, 8, 23, 16, 0, tzinfo=UTC)
    session = TradingSession(
        open_at=datetime(2026, 8, 23, 13, 30, tzinfo=UTC),
        close_at=datetime(2026, 8, 23, 20, 0, tzinfo=UTC),
        trading_day=False,
    )
    state = ContinuousAnalysisState(chart_candidates=[{"symbol": "TSLA.US"}])
    plan = plan_analysis_cycle(now, session, state)
    assert plan.phase is MarketPhase.CLOSED
    assert plan.focus_refresh is False


def test_force_full_runs_same_pipeline_on_demand_when_market_closed() -> None:
    now = datetime(2026, 8, 23, 16, 0, tzinfo=UTC)
    session = TradingSession(
        open_at=datetime(2026, 8, 23, 13, 30, tzinfo=UTC),
        close_at=datetime(2026, 8, 23, 20, 0, tzinfo=UTC),
        trading_day=False,
    )
    state = ContinuousAnalysisState()
    plan = plan_analysis_cycle(now, session, state, force_full=True)
    assert plan.full_scan is True
    assert plan.focus_refresh is True
    assert "forced_full_analysis" in plan.reasons


def test_postmarket_due_cycle_requests_outcome_checkpoint() -> None:
    now = datetime(2026, 8, 24, 21, 0, tzinfo=UTC)
    state = ContinuousAnalysisState(
        last_full_scan_at=(now - timedelta(hours=2)).isoformat(),
        last_focus_scan_at=(now - timedelta(hours=2)).isoformat(),
        chart_candidates=[{"symbol": "NVDA.US"}],
    )
    plan = plan_analysis_cycle(now, _session(), state)
    assert plan.phase is MarketPhase.POSTMARKET
    assert plan.checkpoint_outcomes is True


def test_atomic_state_store_round_trip(tmp_path) -> None:
    path = tmp_path / "state.json"
    store = AtomicAnalysisStateStore(path)
    state = ContinuousAnalysisState(
        cycle=7,
        focus_symbols=["AAPL.US", "TSLA.US"],
        latest_decision="RESEARCH_CANDIDATES",
        latest_shortlist=[{"symbol": "TSLA.US", "ranking_score": 84.2}],
    )
    store.save(state)
    loaded = store.load()
    assert loaded.cycle == 7
    assert loaded.focus_symbols == ["AAPL.US", "TSLA.US"]
    assert loaded.latest_shortlist[0]["symbol"] == "TSLA.US"


def test_weekday_fallback_is_explicitly_non_authoritative() -> None:
    monday = datetime(2026, 8, 24, 15, 0, tzinfo=UTC)
    session = weekday_regular_session(monday)
    assert session.trading_day is True
    assert session.authoritative is False
    assert session.source == "weekday_fallback"


def test_config_rejects_focus_breadth_inversion() -> None:
    cfg = ContinuousScanConfig(max_focus_symbols=20, max_deep_symbols=30)
    try:
        cfg.validate()
    except ValueError as exc:
        assert "max_deep_symbols" in str(exc)
    else:
        raise AssertionError("expected validation error")
