"""Option-contract quality and volatility diagnostics for long-premium research.

This module deliberately separates contract quality from directional conviction.
A strong stock setup can still be a poor option trade when the spread is wide,
open interest is thin, theta is excessive, implied volatility is extremely rich,
or the target move is implausible relative to the underlying's broader option
surface.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any, Mapping


@dataclass(frozen=True)
class OptionQualityConfig:
    min_dte: int = 7
    max_dte: int = 60
    max_spread_pct: float = 12.0
    min_open_interest: int = 100
    min_volume: int = 10
    preferred_abs_delta_min: float = 0.20
    preferred_abs_delta_max: float = 0.65
    max_theta_pct_of_premium_per_day: float = 8.0
    expensive_iv_percentile: float = 85.0
    max_iv_to_rv_ratio_without_catalyst: float = 2.25
    max_required_move_vs_surface_expected: float = 2.0
    weak_surface_efficiency_score: float = 35.0
    extreme_iv_premium_to_atm_points: float = 20.0
    min_quality_score: float = 60.0

    def validate(self) -> None:
        if self.min_dte < 1 or self.max_dte < self.min_dte:
            raise ValueError("invalid DTE range")
        if not 0 < self.max_spread_pct <= 100:
            raise ValueError("max_spread_pct must be > 0 and <= 100")
        if self.min_open_interest < 0 or self.min_volume < 0:
            raise ValueError("liquidity minimums cannot be negative")
        if not 0 < self.preferred_abs_delta_min < self.preferred_abs_delta_max <= 1:
            raise ValueError("invalid preferred delta range")
        if self.max_theta_pct_of_premium_per_day <= 0:
            raise ValueError("max_theta_pct_of_premium_per_day must be positive")
        if not 0 <= self.expensive_iv_percentile <= 100:
            raise ValueError("expensive_iv_percentile must be 0..100")
        if self.max_iv_to_rv_ratio_without_catalyst <= 0:
            raise ValueError("max_iv_to_rv_ratio_without_catalyst must be positive")
        if self.max_required_move_vs_surface_expected <= 0:
            raise ValueError("max_required_move_vs_surface_expected must be positive")
        if not 0 <= self.weak_surface_efficiency_score <= 100:
            raise ValueError("weak_surface_efficiency_score must be 0..100")
        if self.extreme_iv_premium_to_atm_points <= 0:
            raise ValueError("extreme_iv_premium_to_atm_points must be positive")
        if not 0 <= self.min_quality_score <= 100:
            raise ValueError("min_quality_score must be 0..100")


def assess_option_quality(
    candidate: Mapping[str, Any],
    *,
    catalyst_score: float | None = None,
    realized_vol_pct: float | None = None,
    iv_percentile: float | None = None,
    config: OptionQualityConfig | None = None,
) -> dict[str, Any]:
    """Score liquidity, Greeks and volatility economics for one long option."""
    cfg = config or OptionQualityConfig()
    cfg.validate()

    bid = _nonnegative(candidate.get("bid"))
    ask = _positive(candidate.get("entry_ask") or candidate.get("ask"))
    dte = _integer(candidate.get("dte"))
    oi = _integer(candidate.get("open_interest"), default=0) or 0
    volume = _integer(candidate.get("volume"), default=0) or 0
    iv_raw = _positive(candidate.get("implied_volatility"))
    iv_pct = None if iv_raw is None else (iv_raw * 100.0 if iv_raw <= 5.0 else iv_raw)
    delta = _finite(candidate.get("delta"))
    theta = _finite(candidate.get("theta"))
    gamma = _finite(candidate.get("gamma"))
    vega = _finite(candidate.get("vega"))
    catalyst = _bounded(catalyst_score)
    rv = _positive(realized_vol_pct)

    surface_context = candidate.get("surface_context") if isinstance(candidate.get("surface_context"), Mapping) else {}
    surface_efficiency = _bounded(
        candidate.get("surface_efficiency_score")
        if candidate.get("surface_efficiency_score") is not None
        else surface_context.get("surface_efficiency_score")
    )
    surface_move_ratio = _positive(
        candidate.get("surface_required_move_ratio")
        if candidate.get("surface_required_move_ratio") is not None
        else surface_context.get("required_move_vs_surface_expected_move")
    )
    iv_premium_to_atm = _finite(surface_context.get("candidate_iv_premium_to_atm_points"))
    surface_iv_percentile = _bounded(candidate.get("surface_iv_percentile") or surface_context.get("surface_iv_percentile"))
    iv_rank = _bounded(iv_percentile) if iv_percentile is not None else surface_iv_percentile

    hard_reasons: list[str] = []
    warnings: list[str] = []
    if ask is None or bid is None or ask < bid:
        hard_reasons.append("invalid_bid_ask")
        spread_pct = None
    else:
        midpoint = (bid + ask) / 2.0
        spread_pct = ((ask - bid) / midpoint * 100.0) if midpoint > 0 else math.inf
        if spread_pct > cfg.max_spread_pct:
            hard_reasons.append("spread_too_wide")
    if dte is None or not cfg.min_dte <= dte <= cfg.max_dte:
        hard_reasons.append("dte_outside_supported_range")
    if oi < cfg.min_open_interest:
        hard_reasons.append("open_interest_below_minimum")
    if volume < cfg.min_volume:
        warnings.append("volume_below_preference")
    if iv_pct is None:
        warnings.append("implied_volatility_missing")

    abs_delta = abs(delta) if delta is not None else None
    if abs_delta is None:
        warnings.append("delta_missing")
    elif not cfg.preferred_abs_delta_min <= abs_delta <= cfg.preferred_abs_delta_max:
        warnings.append("delta_outside_preferred_range")

    theta_burden = None
    if theta is not None and ask is not None and ask > 0:
        theta_burden = abs(theta) / ask * 100.0
        if theta_burden > cfg.max_theta_pct_of_premium_per_day:
            warnings.append("high_daily_theta_burden")

    iv_to_rv = None
    if iv_pct is not None and rv is not None and rv > 0:
        iv_to_rv = iv_pct / rv
        if iv_to_rv > cfg.max_iv_to_rv_ratio_without_catalyst and (catalyst or 0.0) < 70.0:
            warnings.append("implied_volatility_rich_vs_realized_without_strong_catalyst")
    if iv_rank is not None and iv_rank >= cfg.expensive_iv_percentile:
        warnings.append("iv_percentile_expensive")

    if surface_efficiency is None:
        warnings.append("volatility_surface_context_missing")
    elif surface_efficiency < cfg.weak_surface_efficiency_score:
        warnings.append("surface_efficiency_weak")
    if iv_premium_to_atm is not None and iv_premium_to_atm >= cfg.extreme_iv_premium_to_atm_points:
        warnings.append("contract_iv_extreme_vs_same_expiry_atm")
    if surface_move_ratio is not None and surface_move_ratio > cfg.max_required_move_vs_surface_expected:
        hard_reasons.append("required_move_extreme_vs_surface_expected_move")

    spread_score = 0.0 if spread_pct is None or not math.isfinite(spread_pct) else 30.0 * max(
        0.0, 1.0 - spread_pct / cfg.max_spread_pct
    )
    oi_score = 18.0 * min(1.0, math.log10(max(1, oi) + 1.0) / 4.0)
    volume_score = 10.0 * min(1.0, math.log10(max(1, volume) + 1.0) / 3.0)

    if abs_delta is None:
        delta_score = 6.0
    elif cfg.preferred_abs_delta_min <= abs_delta <= cfg.preferred_abs_delta_max:
        center = (cfg.preferred_abs_delta_min + cfg.preferred_abs_delta_max) / 2.0
        half = (cfg.preferred_abs_delta_max - cfg.preferred_abs_delta_min) / 2.0
        delta_score = 16.0 * max(0.65, 1.0 - abs(abs_delta - center) / max(half, 1e-9) * 0.35)
    else:
        delta_score = 5.0

    if theta_burden is None:
        theta_score = 7.0
    else:
        theta_score = 12.0 * max(0.0, 1.0 - theta_burden / cfg.max_theta_pct_of_premium_per_day)

    volatility_score = 14.0
    if iv_to_rv is not None:
        volatility_score *= max(0.15, min(1.0, 1.5 / max(0.5, iv_to_rv)))
    if iv_rank is not None and iv_rank >= cfg.expensive_iv_percentile:
        volatility_score *= 0.65
    if catalyst is not None and catalyst >= 70.0:
        volatility_score = min(14.0, volatility_score + 3.0)

    surface_adjustment = 0.0
    if surface_efficiency is not None:
        # Surface diagnostics are new and intentionally have only a small bounded
        # influence until retrospective attribution supports stronger weighting.
        surface_adjustment = max(-5.0, min(5.0, (surface_efficiency - 50.0) * 0.10))

    quality_score = (
        spread_score
        + oi_score
        + volume_score
        + delta_score
        + theta_score
        + volatility_score
        + surface_adjustment
    )
    quality_score = min(100.0, max(0.0, quality_score))
    passed = not hard_reasons and quality_score >= cfg.min_quality_score

    return {
        "passed": passed,
        "decision": "OPTION_QUALITY_PASS" if passed else "OPTION_QUALITY_REJECT",
        "quality_score": round(quality_score, 2),
        "hard_reasons": hard_reasons,
        "warnings": warnings,
        "metrics": {
            "spread_pct": None if spread_pct is None or not math.isfinite(spread_pct) else round(spread_pct, 4),
            "open_interest": oi,
            "volume": volume,
            "dte": dte,
            "implied_volatility_pct": None if iv_pct is None else round(iv_pct, 4),
            "iv_percentile": iv_rank,
            "realized_vol_pct": rv,
            "iv_to_realized_vol": None if iv_to_rv is None else round(iv_to_rv, 4),
            "delta": delta,
            "gamma": gamma,
            "theta": theta,
            "vega": vega,
            "theta_pct_of_premium_per_day": None if theta_burden is None else round(theta_burden, 4),
            "surface_efficiency_score": surface_efficiency,
            "surface_required_move_ratio": surface_move_ratio,
            "candidate_iv_premium_to_atm_points": iv_premium_to_atm,
            "surface_term_structure_state": surface_context.get("term_structure_state"),
            "surface_skew_state": surface_context.get("skew_state"),
            "surface_implied_vs_realized_state": surface_context.get("implied_vs_realized_state"),
        },
        "components": {
            "spread": round(spread_score, 2),
            "open_interest": round(oi_score, 2),
            "volume": round(volume_score, 2),
            "delta": round(delta_score, 2),
            "theta": round(theta_score, 2),
            "volatility": round(volatility_score, 2),
            "surface_adjustment": round(surface_adjustment, 2),
        },
        "surface_context": dict(surface_context),
        "config": asdict(cfg),
        "interpretation": "contract-quality heuristic only; not probability or expected return",
    }


def _finite(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _positive(value: object) -> float | None:
    number = _finite(value)
    return number if number is not None and number > 0 else None


def _nonnegative(value: object) -> float | None:
    number = _finite(value)
    return number if number is not None and number >= 0 else None


def _integer(value: object, default: int | None = None) -> int | None:
    number = _finite(value)
    if number is None:
        return default
    if int(number) != number:
        return default
    return int(number)


def _bounded(value: object) -> float | None:
    number = _finite(value)
    if number is None:
        return None
    return min(100.0, max(0.0, number))


__all__ = ["OptionQualityConfig", "assess_option_quality"]
