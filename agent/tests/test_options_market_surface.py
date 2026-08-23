from __future__ import annotations

import pytest

from src.options_market.surface import (
    VolatilitySurfaceConfig,
    analyze_volatility_surface,
    contract_surface_context,
)


def _contract(
    option_type: str,
    strike: float,
    expiration: str,
    dte: int,
    iv: float,
    delta: float,
    *,
    bid: float = 2.0,
    ask: float = 2.2,
    oi: int = 500,
) -> dict:
    token = "C" if option_type == "call" else "P"
    return {
        "contract_symbol": f"XYZ{expiration.replace('-', '')[2:]}{token}{int(strike * 1000):08d}",
        "option_type": option_type,
        "strike": strike,
        "expiration": expiration,
        "dte": dte,
        "bid": bid,
        "ask": ask,
        "implied_volatility": iv,
        "delta": delta,
        "open_interest": oi,
    }


def _surface_rows() -> list[dict]:
    return [
        _contract("call", 100, "2026-09-18", 25, 0.60, 0.52, bid=5.0, ask=5.2),
        _contract("put", 100, "2026-09-18", 25, 0.62, -0.48, bid=4.7, ask=4.9),
        _contract("call", 110, "2026-09-18", 25, 0.58, 0.25, bid=1.8, ask=2.0),
        _contract("put", 90, "2026-09-18", 25, 0.68, -0.25, bid=1.6, ask=1.8),
        _contract("call", 100, "2026-10-16", 53, 0.40, 0.53, bid=7.0, ask=7.3),
        _contract("put", 100, "2026-10-16", 53, 0.42, -0.47, bid=6.4, ask=6.7),
        _contract("call", 115, "2026-10-16", 53, 0.39, 0.25, bid=2.0, ask=2.2),
        _contract("put", 85, "2026-10-16", 53, 0.45, -0.25, bid=1.7, ask=1.9),
    ]


def test_surface_detects_front_loaded_iv_put_skew_and_rich_iv() -> None:
    report = analyze_volatility_surface(_surface_rows(), spot=100.0, realized_vol_pct=30.0)
    assert report["status"] == "ok"
    assert report["expirations_usable"] == 2
    assert report["term_structure"]["state"] == "front_loaded"
    assert report["term_structure"]["back_minus_front_iv_points"] < -15.0
    assert report["skew"]["state"] == "put_skew"
    assert report["skew"]["median_put_minus_call_25d_iv_points"] >= 5.0
    assert report["implied_vs_realized"]["state"] == "rich"


def test_expiry_summary_uses_atm_iv_and_straddle_mid_move() -> None:
    report = analyze_volatility_surface(_surface_rows(), spot=100.0)
    front = report["expirations"][0]
    assert front["atm_strike"] == 100.0
    assert front["atm_iv_pct"] == 61.0
    assert front["atm_expected_move_pct"] > 15.0
    assert front["straddle_mid_move_pct"] == pytest.approx(9.9, abs=0.001)
    assert front["put_call_25d_skew_vol_points"] == 10.0


def test_contract_context_compares_wing_to_same_expiry_surface() -> None:
    surface = analyze_volatility_surface(_surface_rows(), spot=100.0, realized_vol_pct=30.0)
    candidate = {
        "expiration": "2026-09-18",
        "implied_volatility": 0.78,
        "entry_ask": 2.0,
        "spot": 100.0,
        "delta": 0.25,
        "theta": -0.06,
        "spread_pct": 10.0,
        "required_underlying_move_pct": 30.0,
    }
    context = contract_surface_context(candidate, surface)
    assert context["surface_context_available"] is True
    assert context["candidate_iv_premium_to_atm_points"] == 17.0
    assert context["required_move_vs_surface_expected_move"] > 1.5
    assert context["surface_iv_percentile"] == 100.0
    assert 0.0 <= context["surface_efficiency_score"] <= 100.0


def test_surface_degrades_cleanly_when_delta_pairs_are_missing() -> None:
    rows = [
        {**_contract("call", 100, "2026-09-18", 25, 0.4, 0.52), "delta": None},
        {**_contract("put", 100, "2026-09-18", 25, 0.42, -0.48), "delta": None},
    ]
    report = analyze_volatility_surface(rows, spot=100.0)
    assert report["status"] == "ok"
    assert report["skew"]["state"] == "insufficient"
    assert "25_delta_skew_pair_unavailable" in report["warnings"]


def test_invalid_spot_fails_closed() -> None:
    with pytest.raises(ValueError, match="spot"):
        analyze_volatility_surface(_surface_rows(), spot=0.0)


def test_surface_config_rejects_inverted_iv_rv_thresholds() -> None:
    with pytest.raises(ValueError, match="IV/RV"):
        VolatilitySurfaceConfig(cheap_iv_to_rv_ratio=2.0, rich_iv_to_rv_ratio=1.0).validate()
