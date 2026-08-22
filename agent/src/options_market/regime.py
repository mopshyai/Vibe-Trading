"""Small, inspectable U.S. market-regime classifier for personal options research.

The classifier intentionally uses broad benchmark price action and optional
cross-sectional breadth rather than attempting to predict the next market move.
Its output is a context gate for candidate ranking, not a trade signal.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any, Mapping

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class RegimeConfig:
    min_bars: int = 220
    trend_return_days: int = 20
    high_vol_rv20_pct: float = 32.0
    low_vol_rv20_pct: float = 14.0
    strong_breadth_pct: float = 62.0
    weak_breadth_pct: float = 38.0

    def validate(self) -> None:
        if self.min_bars < 200:
            raise ValueError("min_bars must be at least 200")
        if not 5 <= self.trend_return_days <= 63:
            raise ValueError("trend_return_days must be between 5 and 63")
        if not 0 < self.low_vol_rv20_pct < self.high_vol_rv20_pct:
            raise ValueError("volatility thresholds are invalid")
        if not 0 <= self.weak_breadth_pct < self.strong_breadth_pct <= 100:
            raise ValueError("breadth thresholds are invalid")


def classify_market_regime(
    benchmarks: Mapping[str, pd.DataFrame],
    *,
    bullish_breadth_pct: float | None = None,
    config: RegimeConfig | None = None,
) -> dict[str, Any]:
    """Classify broad U.S. conditions from benchmark OHLCV frames.

    Preferred inputs are SPY plus QQQ and/or IWM. Frames need at least a
    ``close`` column and are evaluated only with observations supplied by the
    caller, preserving the point-in-time boundary of the research store.
    """
    cfg = config or RegimeConfig()
    cfg.validate()

    features: dict[str, dict[str, float | bool]] = {}
    for raw_symbol, frame in benchmarks.items():
        symbol = _symbol(raw_symbol)
        snapshot = _benchmark_features(frame, cfg)
        if symbol and snapshot is not None:
            features[symbol] = snapshot

    if not features:
        return _result("unknown", 0.0, features, bullish_breadth_pct, cfg, ["no_valid_benchmark_data"])

    spy = features.get("SPY") or features.get("SPY.US") or next(iter(features.values()))
    all_rows = list(features.values())
    above_50 = float(np.mean([bool(row["above_sma50"]) for row in all_rows]))
    above_200 = float(np.mean([bool(row["above_sma200"]) for row in all_rows]))
    positive_20 = float(np.mean([float(row["return_20d_pct"]) > 0.0 for row in all_rows]))
    rv20 = float(np.mean([float(row["rv20_pct"]) for row in all_rows]))

    breadth = _bounded_pct(bullish_breadth_pct)
    breadth_fraction = None if breadth is None else breadth / 100.0
    trend_up_score = 0.38 * above_50 + 0.42 * above_200 + 0.20 * positive_20
    trend_down_score = 1.0 - trend_up_score
    if breadth_fraction is not None:
        trend_up_score = 0.80 * trend_up_score + 0.20 * breadth_fraction
        trend_down_score = 0.80 * trend_down_score + 0.20 * (1.0 - breadth_fraction)

    spy_ret20 = float(spy["return_20d_pct"])
    spy_above_200 = bool(spy["above_sma200"])
    warnings: list[str] = []

    if rv20 >= cfg.high_vol_rv20_pct:
        if trend_down_score >= 0.58 or spy_ret20 < -3.0:
            regime = "risk_off_high_vol"
            confidence = max(trend_down_score, min(1.0, rv20 / 60.0))
        elif trend_up_score >= 0.60:
            regime = "risk_on_high_vol"
            confidence = max(trend_up_score, min(1.0, rv20 / 60.0))
        else:
            regime = "high_vol_mixed"
            confidence = min(1.0, 0.50 + rv20 / 100.0)
    elif trend_up_score >= 0.66 and spy_above_200 and spy_ret20 > 0.0:
        regime = "risk_on_trend"
        confidence = trend_up_score
    elif trend_down_score >= 0.66 and (not spy_above_200) and spy_ret20 < 0.0:
        regime = "risk_off_trend"
        confidence = trend_down_score
    elif rv20 <= cfg.low_vol_rv20_pct and 0.42 <= trend_up_score <= 0.62:
        regime = "low_vol_range"
        confidence = 0.62
    else:
        regime = "mixed"
        confidence = max(0.50, abs(trend_up_score - 0.5) + 0.5)

    if breadth is None:
        warnings.append("breadth_not_supplied")
    elif breadth >= cfg.strong_breadth_pct and regime.startswith("risk_off"):
        warnings.append("breadth_disagrees_with_benchmark_trend")
    elif breadth <= cfg.weak_breadth_pct and regime.startswith("risk_on"):
        warnings.append("breadth_disagrees_with_benchmark_trend")

    result = _result(regime, confidence, features, breadth, cfg, warnings)
    result["trend_up_score"] = round(trend_up_score * 100.0, 2)
    result["trend_down_score"] = round(trend_down_score * 100.0, 2)
    result["average_rv20_pct"] = round(rv20, 2)
    return result


def regime_direction_fit(regime_report: Mapping[str, Any], direction: str) -> dict[str, Any]:
    """Return an explainable 0..100 context fit for bullish/bearish candidates."""
    direction = str(direction or "").strip().lower()
    regime = str(regime_report.get("regime") or "unknown")
    if direction not in {"bullish", "bearish"}:
        return {"score": 0.0, "aligned": False, "reason": "invalid_direction"}

    if regime in {"unknown", "mixed", "high_vol_mixed"}:
        score = 50.0 if regime != "unknown" else 35.0
    elif regime.startswith("risk_on"):
        score = 85.0 if direction == "bullish" else 25.0
    elif regime.startswith("risk_off"):
        score = 85.0 if direction == "bearish" else 25.0
    elif regime == "low_vol_range":
        score = 45.0
    else:
        score = 50.0

    confidence = _finite(regime_report.get("confidence"))
    if confidence is not None:
        confidence = min(1.0, max(0.0, confidence))
        score = 50.0 + (score - 50.0) * confidence
    return {
        "score": round(min(100.0, max(0.0, score)), 2),
        "aligned": score >= 60.0,
        "regime": regime,
        "direction": direction,
    }


def _benchmark_features(frame: pd.DataFrame, cfg: RegimeConfig) -> dict[str, float | bool] | None:
    if not isinstance(frame, pd.DataFrame) or "close" not in frame.columns:
        return None
    close = pd.to_numeric(frame["close"], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    if len(close) < cfg.min_bars or (close <= 0).any():
        return None
    sma50 = float(close.iloc[-50:].mean())
    sma200 = float(close.iloc[-200:].mean())
    last = float(close.iloc[-1])
    base = float(close.iloc[-1 - cfg.trend_return_days])
    ret20 = (last / base - 1.0) * 100.0 if base > 0 else 0.0
    returns = np.log(close / close.shift(1)).dropna().iloc[-20:]
    rv20 = float(returns.std(ddof=0) * math.sqrt(252.0) * 100.0) if len(returns) >= 2 else 0.0
    return {
        "close": round(last, 4),
        "sma50": round(sma50, 4),
        "sma200": round(sma200, 4),
        "above_sma50": last > sma50,
        "above_sma200": last > sma200,
        "return_20d_pct": round(ret20, 4),
        "rv20_pct": round(rv20, 4),
    }


def _result(
    regime: str,
    confidence: float,
    features: Mapping[str, Any],
    breadth: float | None,
    cfg: RegimeConfig,
    warnings: list[str],
) -> dict[str, Any]:
    return {
        "regime": regime,
        "confidence": round(min(1.0, max(0.0, confidence)), 4),
        "bullish_breadth_pct": breadth,
        "benchmarks": dict(features),
        "warnings": warnings,
        "config": asdict(cfg),
        "interpretation": "market context only; not a prediction or standalone trade signal",
    }


def _symbol(value: object) -> str:
    return str(value or "").strip().upper()


def _bounded_pct(value: object) -> float | None:
    number = _finite(value)
    if number is None:
        return None
    return min(100.0, max(0.0, number))


def _finite(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


__all__ = ["RegimeConfig", "classify_market_regime", "regime_direction_fit"]
