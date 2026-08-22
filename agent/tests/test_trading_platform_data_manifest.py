from __future__ import annotations

from datetime import datetime, timezone

from src.trading_platform import DataPlaneManifest


def test_manifest_round_trips_success_and_error(tmp_path) -> None:
    path = tmp_path / "data-health.json"
    manifest = DataPlaneManifest(path)
    now = datetime(2026, 8, 24, 14, 0, tzinfo=timezone.utc)

    manifest.mark_success(
        "equity_universe",
        observed_at=now,
        source="nasdaq_trader_symbol_directory",
        metadata={"count": 5500},
    )
    manifest.mark_error(
        "options_market",
        "feed unavailable",
        observed_at=now,
        source="alpaca:opra",
    )

    rows = manifest.read()
    assert rows["equity_universe"]["source"] == "nasdaq_trader_symbol_directory"
    assert rows["equity_universe"]["metadata"]["count"] == 5500
    assert rows["options_market"]["error"] == "feed unavailable"


def test_manifest_rejects_unsafe_component_name(tmp_path) -> None:
    manifest = DataPlaneManifest(tmp_path / "data-health.json")
    try:
        manifest.mark_success("Options Market")
    except ValueError as exc:
        assert "component" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("invalid component name should fail closed")
