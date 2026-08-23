from __future__ import annotations

from src.options_market.volatility import assess_option_quality


def _candidate(**overrides):
    row = {
        "bid": 4.9,
        "entry_ask": 5.0,
        "dte": 30,
        "open_interest": 1500,
        "volume": 500,
        "implied_volatility": 0.40,
        "delta": 0.40,
        "gamma": 0.02,
        "theta": -0.10,
        "vega": 0.12,
    }
    row.update(overrides)
    return row


def test_surface_move_extreme_becomes_hard_feasibility_reject() -> None:
    report = assess_option_quality(
        _candidate(
            surface_efficiency_score=45.0,
            surface_required_move_ratio=2.5,
            surface_iv_percentile=55.0,
            surface_context={
                "surface_efficiency_score": 45.0,
                "required_move_vs_surface_expected_move": 2.5,
                "candidate_iv_premium_to_atm_points": 5.0,
                "term_structure_state": "flat",
                "skew_state": "balanced",
            },
        )
    )
    assert report["passed"] is False
    assert "required_move_extreme_vs_surface_expected_move" in report["hard_reasons"]


def test_rich_wing_iv_is_warning_not_unvalidated_hard_gate() -> None:
    report = assess_option_quality(
        _candidate(
            surface_efficiency_score=55.0,
            surface_required_move_ratio=1.1,
            surface_iv_percentile=90.0,
            surface_context={
                "surface_efficiency_score": 55.0,
                "required_move_vs_surface_expected_move": 1.1,
                "candidate_iv_premium_to_atm_points": 22.0,
                "surface_iv_percentile": 90.0,
                "term_structure_state": "front_loaded",
                "skew_state": "put_skew",
            },
        )
    )
    assert "contract_iv_extreme_vs_same_expiry_atm" in report["warnings"]
    assert "iv_percentile_expensive" in report["warnings"]
    assert "required_move_extreme_vs_surface_expected_move" not in report["hard_reasons"]


def test_missing_surface_context_remains_backward_compatible_warning() -> None:
    report = assess_option_quality(_candidate())
    assert "volatility_surface_context_missing" in report["warnings"]
    assert "required_move_extreme_vs_surface_expected_move" not in report["hard_reasons"]
    assert report["metrics"]["surface_efficiency_score"] is None


def test_surface_efficiency_only_has_bounded_score_influence() -> None:
    weak = assess_option_quality(
        _candidate(
            surface_efficiency_score=0.0,
            surface_required_move_ratio=1.0,
            surface_context={"surface_efficiency_score": 0.0},
        )
    )
    strong = assess_option_quality(
        _candidate(
            surface_efficiency_score=100.0,
            surface_required_move_ratio=1.0,
            surface_context={"surface_efficiency_score": 100.0},
        )
    )
    assert strong["quality_score"] - weak["quality_score"] <= 10.0
    assert weak["components"]["surface_adjustment"] == -5.0
    assert strong["components"]["surface_adjustment"] == 5.0
