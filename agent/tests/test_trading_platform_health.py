from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.trading_platform import FreshnessPolicy, HealthStatus, evaluate_data_freshness


def test_fresh_required_component_is_healthy() -> None:
    now = datetime(2026, 8, 24, 14, 0, tzinfo=timezone.utc)
    report = evaluate_data_freshness(
        {"options_market": {"observed_at": (now - timedelta(seconds=10)).isoformat(), "source": "opra"}},
        now=now,
        policies={"options_market": FreshnessPolicy(max_age_seconds=60, blocking=True)},
    )
    assert report.healthy
    assert report.components[0].status is HealthStatus.OK


def test_stale_required_component_blocks() -> None:
    now = datetime(2026, 8, 24, 14, 0, tzinfo=timezone.utc)
    report = evaluate_data_freshness(
        {"options_market": {"observed_at": (now - timedelta(minutes=3)).isoformat(), "source": "opra"}},
        now=now,
        policies={"options_market": FreshnessPolicy(max_age_seconds=60, blocking=True)},
    )
    assert not report.healthy
    assert report.components[0].status is HealthStatus.STALE
    assert report.blocking_reasons == ["options_market:stale"]


def test_missing_required_component_fails_closed() -> None:
    report = evaluate_data_freshness(
        {},
        policies={"equity_market": FreshnessPolicy(max_age_seconds=120, blocking=True)},
    )
    assert not report.healthy
    assert report.components[0].status is HealthStatus.UNKNOWN
