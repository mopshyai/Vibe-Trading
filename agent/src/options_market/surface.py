"""Provider-independent option volatility-surface diagnostics.

The surface layer compares contracts across strikes and expirations for one
underlying. It is research context only: skew, term structure and efficiency
scores are diagnostics, not probabilities, expected returns or order signals.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import math
from statistics import median
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class VolatilitySurfaceConfig:
    delta_target: float = 0.25
    delta_tolerance: float = 0.15
    max_atm_moneyness_pct: float = 7.5
    skew_alert_vol_points: float = 5.0
    term_structure_alert_vol_points: float = 5.0
    rich_iv_to_rv_ratio: float = 1.50
    cheap_iv_to_rv_ratio: float = 0.90
    extreme_contract_iv_premium_points: float = 20.0
    max_required_move_vs_surface_expected: float = 2.0

    def validate(self) -> None:
        if not 0 < self.delta_target < 1:
            raise ValueError("delta_target must be between 0 and 1")
        if not 0 < self.delta_tolerance < 0.5:
            raise ValueError("delta_tolerance must be between 0 and 0.5")
        if not 0 < self.max_atm_moneyness_pct <= 25:
            raise ValueError("max_atm_moneyness_pct must be > 0 and <= 25")
        if self.skew_alert_vol_points < 0 or self.term_structure_alert_vol_points < 0:
            raise ValueError("surface alert thresholds cannot be negative")
        if not 0 < self.cheap_iv_to_rv_ratio < self.rich_iv_to_rv_ratio:
            raise ValueError("IV/RV thresholds are invalid")
        if self.extreme_contract_iv_premium_points <= 0:
            raise ValueError("extreme_contract_iv_premium_points must be positive")
        if self.max_required_move_vs_surface_expected <= 0:
            raise ValueError("max_required_move_vs_surface_expected must be positive")


def analyze_volatility_surface(
    contracts: Iterable[Mapping[str, Any]],
    *,
    spot: float,
    realized_vol_pct: float | None = None,
    config: VolatilitySurfaceConfig | None = None,
) -> dict[str, Any]:
    """Summarize ATM IV, expected move, 25-delta skew and IV term structure."""
    cfg = config or VolatilitySurfaceConfig()
    cfg.validate()
    spot_value = _positive(spot)
    if spot_value is None:
        raise ValueError("spot must be a positive finite number")

    rows = [_normalize_contract(row, spot_value) for row in contracts]
    normalized = [row for row in rows if row is not None]
    if not normalized:
        return _empty_surface(spot_value, realized_vol_pct, cfg, "no_usable_option_surface_rows")

    expirations: list[dict[str, Any]] = []
    expiration_keys = sorted({str(row["expiration"]) for row in normalized})
    for expiration in expiration_keys:
        group = [row for row in normalized if row["expiration"] == expiration]
        summary = _expiration_summary(group, spot_value, cfg)
        if summary is not None:
            expirations.append(summary)
    expirations.sort(key=lambda row: (int(row["dte"]), str(row["expiration"])))

    atm_rows = [row for row in expirations if row.get("atm_iv_pct") is not None]
    near = atm_rows[0] if atm_rows else None
    far = atm_rows[-1] if len(atm_rows) >= 2 else None
    term_change = None
    term_slope_30d = None
    term_state = "insufficient"
    if near is not None and far is not None and int(far["dte"]) > int(near["dte"]):
        term_change = float(far["atm_iv_pct"]) - float(near["atm_iv_pct"])
        term_slope_30d = term_change / (int(far["dte"]) - int(near["dte"])) * 30.0
        if term_change >= cfg.term_structure_alert_vol_points:
            term_state = "back_loaded"
        elif term_change <= -cfg.term_structure_alert_vol_points:
            term_state = "front_loaded"
        else:
            term_state = "flat"

    skew_values = [float(row["put_call_25d_skew_vol_points"]) for row in expirations if row.get("put_call_25d_skew_vol_points") is not None]
    median_skew = median(skew_values) if skew_values else None
    if median_skew is None:
        skew_state = "insufficient"
    elif median_skew >= cfg.skew_alert_vol_points:
        skew_state = "put_skew"
    elif median_skew <= -cfg.skew_alert_vol_points:
        skew_state = "call_skew"
    else:
        skew_state = "balanced"

    rv = _positive(realized_vol_pct)
    front_iv_to_rv = None
    implied_vs_realized = "unknown"
    if near is not None and rv is not None:
        front_iv_to_rv = float(near["atm_iv_pct"]) / rv
        if front_iv_to_rv >= cfg.rich_iv_to_rv_ratio:
            implied_vs_realized = "rich"
        elif front_iv_to_rv <= cfg.cheap_iv_to_rv_ratio:
            implied_vs_realized = "cheap"
        else:
            implied_vs_realized = "balanced"

    warnings: list[str] = []
    if len(atm_rows) < 2:
        warnings.append("term_structure_requires_multiple_usable_expirations")
    if not skew_values:
        warnings.append("25_delta_skew_pair_unavailable")
    if rv is None:
        warnings.append("realized_volatility_not_supplied")

    return {
        "status": "ok",
        "spot": round(spot_value, 6),
        "contracts_received": len(rows),
        "contracts_usable": len(normalized),
        "expirations_usable": len(expirations),
        "expirations": expirations,
        "term_structure": {
            "state": term_state,
            "front_expiration": None if near is None else near["expiration"],
            "back_expiration": None if far is None else far["expiration"],
            "front_atm_iv_pct": None if near is None else near["atm_iv_pct"],
            "back_atm_iv_pct": None if far is None else far["atm_iv_pct"],
            "back_minus_front_iv_points": _round(term_change),
            "slope_vol_points_per_30d": _round(term_slope_30d),
        },
        "skew": {
            "state": skew_state,
            "median_put_minus_call_25d_iv_points": _round(median_skew),
            "expirations_with_25d_pair": len(skew_values),
        },
        "implied_vs_realized": {
            "state": implied_vs_realized,
            "realized_vol_pct": _round(rv),
            "front_atm_iv_to_rv": _round(front_iv_to_rv),
        },
        "warnings": warnings,
        "config": asdict(cfg),
        "interpretation": "volatility-surface research context only; not a probability, forecast or order signal",
    }


def contract_surface_context(
    candidate: Mapping[str, Any],
    surface: Mapping[str, Any],
    *,
    config: VolatilitySurfaceConfig | None = None,
) -> dict[str, Any]:
    """Project one contract into its expiry surface and score relative efficiency."""
    cfg = config or VolatilitySurfaceConfig()
    cfg.validate()
    expiration = str(candidate.get("expiration") or "")[:10]
    expiry_rows = surface.get("expirations") if isinstance(surface.get("expirations"), list) else []
    expiry = next((row for row in expiry_rows if isinstance(row, Mapping) and str(row.get("expiration")) == expiration), None)
    if not isinstance(expiry, Mapping):
        return {
            "surface_context_available": False,
            "surface_efficiency_score": None,
            "warnings": ["candidate_expiration_missing_from_surface"],
        }

    candidate_iv = _iv_pct(candidate.get("implied_volatility"))
    atm_iv = _positive(expiry.get("atm_iv_pct"))
    required_move = _nonnegative(candidate.get("required_underlying_move_pct"))
    surface_move = _positive(expiry.get("atm_expected_move_pct"))
    iv_premium = None if candidate_iv is None or atm_iv is None else candidate_iv - atm_iv
    required_vs_surface = None if required_move is None or surface_move is None else required_move / surface_move

    ask = _positive(candidate.get("entry_ask") or candidate.get("ask"))
    spot = _positive(candidate.get("spot") or surface.get("spot"))
    delta = _finite(candidate.get("delta"))
    theta = _finite(candidate.get("theta"))
    delta_notional_per_premium = None
    if ask is not None and spot is not None and delta is not None:
        delta_notional_per_premium = abs(delta) * spot / ask
    theta_burden = None
    if ask is not None and theta is not None:
        theta_burden = abs(theta) / ask * 100.0

    iv_score = 25.0
    if iv_premium is not None:
        if iv_premium <= 0:
            iv_score = 25.0
        else:
            iv_score = 25.0 * max(0.0, 1.0 - iv_premium / cfg.extreme_contract_iv_premium_points)
    move_score = 35.0
    if required_vs_surface is not None:
        move_score = 35.0 * max(0.0, 1.0 - required_vs_surface / cfg.max_required_move_vs_surface_expected)
    delta_score = 15.0
    if delta_notional_per_premium is not None:
        delta_score = 15.0 * min(1.0, max(0.0, delta_notional_per_premium / 12.0))
    theta_score = 15.0
    if theta_burden is not None:
        theta_score = 15.0 * max(0.0, 1.0 - theta_burden / 10.0)
    spread = _nonnegative(candidate.get("spread_pct"))
    spread_score = 10.0 if spread is None else 10.0 * max(0.0, 1.0 - spread / 15.0)
    efficiency = min(100.0, max(0.0, iv_score + move_score + delta_score + theta_score + spread_score))

    all_ivs = [_positive(row.get("implied_volatility_pct")) for row in expiry.get("contracts", []) if isinstance(row, Mapping)]
    ivs = sorted(value for value in all_ivs if value is not None)
    iv_percentile = None
    if candidate_iv is not None and ivs:
        below_or_equal = sum(1 for value in ivs if value <= candidate_iv)
        iv_percentile = below_or_equal / len(ivs) * 100.0

    warnings: list[str] = []
    if iv_premium is not None and iv_premium >= cfg.extreme_contract_iv_premium_points:
        warnings.append("contract_iv_extreme_vs_same_expiry_atm")
    if required_vs_surface is not None and required_vs_surface > cfg.max_required_move_vs_surface_expected:
        warnings.append("required_move_extreme_vs_surface_expected_move")

    return {
        "surface_context_available": True,
        "expiration": expiration,
        "atm_iv_pct": _round(atm_iv),
        "atm_expected_move_pct": _round(surface_move),
        "straddle_mid_move_pct": expiry.get("straddle_mid_move_pct"),
        "put_call_25d_skew_vol_points": expiry.get("put_call_25d_skew_vol_points"),
        "candidate_iv_pct": _round(candidate_iv),
        "candidate_iv_premium_to_atm_points": _round(iv_premium),
        "required_move_vs_surface_expected_move": _round(required_vs_surface),
        "delta_notional_per_premium": _round(delta_notional_per_premium),
        "theta_pct_of_premium_per_day": _round(theta_burden),
        "surface_iv_percentile": _round(iv_percentile),
        "surface_efficiency_score": round(efficiency, 2),
        "term_structure_state": _nested(surface, "term_structure", "state"),
        "skew_state": _nested(surface, "skew", "state"),
        "implied_vs_realized_state": _nested(surface, "implied_vs_realized", "state"),
        "warnings": warnings,
        "interpretation": "relative contract/surface efficiency heuristic; not expected return or probability",
    }


def _expiration_summary(rows: list[dict[str, Any]], spot: float, cfg: VolatilitySurfaceConfig) -> dict[str, Any] | None:
    if not rows:
        return None
    dte_values = [int(row["dte"]) for row in rows]
    dte = min(dte_values)
    if dte <= 0:
        return None
    strikes = sorted({float(row["strike"]) for row in rows})
    atm_strike = min(strikes, key=lambda strike: abs(strike - spot))
    moneyness_pct = abs(atm_strike / spot - 1.0) * 100.0
    if moneyness_pct > cfg.max_atm_moneyness_pct:
        return None

    atm_contracts = [row for row in rows if abs(float(row["strike"]) - atm_strike) < 1e-8]
    atm_ivs = [float(row["implied_volatility_pct"]) for row in atm_contracts]
    atm_iv = median(atm_ivs) if atm_ivs else None
    expected_move = None if atm_iv is None else atm_iv * math.sqrt(dte / 365.0)

    call_atm = next((row for row in atm_contracts if row["option_type"] == "call"), None)
    put_atm = next((row for row in atm_contracts if row["option_type"] == "put"), None)
    straddle_move = None
    if call_atm is not None and put_atm is not None:
        call_mid = _mid(call_atm)
        put_mid = _mid(put_atm)
        if call_mid is not None and put_mid is not None:
            straddle_move = (call_mid + put_mid) / spot * 100.0

    call_25 = _nearest_delta(rows, "call", cfg.delta_target, cfg.delta_tolerance)
    put_25 = _nearest_delta(rows, "put", -cfg.delta_target, cfg.delta_tolerance)
    skew = None
    if call_25 is not None and put_25 is not None:
        skew = float(put_25["implied_volatility_pct"]) - float(call_25["implied_volatility_pct"])

    spreads = [float(row["spread_pct"]) for row in rows if row.get("spread_pct") is not None]
    open_interest = sum(int(row.get("open_interest") or 0) for row in rows)
    compact_contracts = [
        {
            "contract_symbol": row.get("contract_symbol"),
            "option_type": row["option_type"],
            "strike": row["strike"],
            "delta": row.get("delta"),
            "implied_volatility_pct": row["implied_volatility_pct"],
        }
        for row in rows
    ]
    return {
        "expiration": rows[0]["expiration"],
        "dte": dte,
        "atm_strike": round(atm_strike, 6),
        "atm_moneyness_pct": round(moneyness_pct, 4),
        "atm_iv_pct": _round(atm_iv),
        "atm_expected_move_pct": _round(expected_move),
        "straddle_mid_move_pct": _round(straddle_move),
        "put_call_25d_skew_vol_points": _round(skew),
        "call_25d_contract": None if call_25 is None else call_25.get("contract_symbol"),
        "put_25d_contract": None if put_25 is None else put_25.get("contract_symbol"),
        "median_spread_pct": _round(median(spreads) if spreads else None),
        "total_open_interest": open_interest,
        "contracts_usable": len(rows),
        "contracts": compact_contracts,
    }


def _normalize_contract(raw: Mapping[str, Any], spot: float) -> dict[str, Any] | None:
    option_type = str(raw.get("option_type") or raw.get("type") or "").strip().lower()
    if option_type not in {"call", "put"}:
        return None
    strike = _positive(raw.get("strike") or raw.get("strike_price"))
    dte = _integer(raw.get("dte"))
    expiration = _expiration(raw.get("expiration") or raw.get("expiration_date"))
    iv = _iv_pct(raw.get("implied_volatility"))
    bid = _nonnegative(raw.get("bid"))
    ask = _positive(raw.get("entry_ask") or raw.get("ask"))
    if strike is None or dte is None or dte <= 0 or expiration is None or iv is None:
        return None
    if ask is not None and bid is not None and ask < bid:
        return None
    midpoint = None if ask is None or bid is None else (ask + bid) / 2.0
    spread_pct = None
    if midpoint is not None and midpoint > 0:
        spread_pct = (ask - bid) / midpoint * 100.0
    return {
        "contract_symbol": str(raw.get("contract_symbol") or raw.get("symbol") or "").strip().upper() or None,
        "option_type": option_type,
        "strike": strike,
        "expiration": expiration,
        "dte": dte,
        "bid": bid,
        "ask": ask,
        "spread_pct": spread_pct,
        "implied_volatility_pct": iv,
        "delta": _finite(raw.get("delta")),
        "open_interest": _integer(raw.get("open_interest"), default=0) or 0,
        "moneyness_pct": (strike / spot - 1.0) * 100.0,
    }


def _nearest_delta(rows: list[dict[str, Any]], option_type: str, target: float, tolerance: float) -> dict[str, Any] | None:
    candidates = [row for row in rows if row["option_type"] == option_type and row.get("delta") is not None]
    if not candidates:
        return None
    selected = min(candidates, key=lambda row: abs(float(row["delta"]) - target))
    return selected if abs(float(selected["delta"]) - target) <= tolerance else None


def _mid(row: Mapping[str, Any]) -> float | None:
    bid = _nonnegative(row.get("bid"))
    ask = _positive(row.get("ask"))
    if bid is None or ask is None or ask < bid:
        return None
    return (bid + ask) / 2.0


def _empty_surface(spot: float, rv: float | None, cfg: VolatilitySurfaceConfig, reason: str) -> dict[str, Any]:
    return {
        "status": "insufficient",
        "spot": round(spot, 6),
        "contracts_received": 0,
        "contracts_usable": 0,
        "expirations_usable": 0,
        "expirations": [],
        "term_structure": {"state": "insufficient"},
        "skew": {"state": "insufficient"},
        "implied_vs_realized": {"state": "unknown", "realized_vol_pct": _round(_positive(rv))},
        "warnings": [reason],
        "config": asdict(cfg),
        "interpretation": "volatility-surface research context only; not a probability, forecast or order signal",
    }


def _nested(value: Mapping[str, Any], key: str, child: str) -> Any:
    nested = value.get(key)
    return nested.get(child) if isinstance(nested, Mapping) else None


def _expiration(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return None


def _iv_pct(value: object) -> float | None:
    number = _positive(value)
    if number is None:
        return None
    return number * 100.0 if number <= 5.0 else number


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
    if number is None or int(number) != number:
        return default
    return int(number)


def _round(value: object) -> float | None:
    number = _finite(value)
    return None if number is None else round(number, 4)


__all__ = ["VolatilitySurfaceConfig", "analyze_volatility_surface", "contract_surface_context"]
