"""Historical outcome labeling and empirical calibration for long-option research.

Outcome labels deliberately use future option quotes and therefore belong only in
backtests/evaluation. They must never be fed back into a point-in-time feature row.

For execution realism, a long option is assumed to enter at the recorded ask and
exit/mark at the recorded bid. This is conservative relative to midpoint fills and
makes spread cost visible in the outcome itself.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class OutcomeConfig:
    """Outcome-label settings for a long-premium candidate."""

    target_profit_pct: float = 300.0
    full_loss_threshold_pct: float = 95.0
    score_bucket_size: int = 10
    min_bucket_samples: int = 30

    @property
    def target_multiple(self) -> float:
        return 1.0 + self.target_profit_pct / 100.0

    def validate(self) -> None:
        if not 0 < self.target_profit_pct <= 10_000:
            raise ValueError("target_profit_pct must be > 0 and <= 10000")
        if not 0 < self.full_loss_threshold_pct < 100:
            raise ValueError("full_loss_threshold_pct must be > 0 and < 100")
        if not 1 <= self.score_bucket_size <= 50:
            raise ValueError("score_bucket_size must be between 1 and 50")
        if self.min_bucket_samples < 1:
            raise ValueError("min_bucket_samples must be at least 1")


def label_long_option_path(
    candidate: Mapping[str, Any],
    quotes: pd.DataFrame,
    *,
    config: OutcomeConfig | None = None,
) -> dict[str, Any]:
    """Label one long option using ask-at-entry and bid-for-exit economics."""
    cfg = config or OutcomeConfig()
    cfg.validate()

    entry_ask = _positive(candidate.get("entry_ask"))
    if entry_ask is None:
        raise ValueError("candidate entry_ask must be a positive finite number")

    frame = _normalize_quotes(quotes)
    if frame.empty:
        raise ValueError("quotes must contain at least one usable bid")

    bid = frame["bid"]
    target_premium = entry_ask * cfg.target_multiple
    max_bid = float(bid.max())
    min_bid = float(bid.min())
    last_bid = float(bid.iloc[-1])

    target_mask = bid >= target_premium
    target_hit = bool(target_mask.any())
    target_touch_time = _index_value(frame.index[target_mask.argmax()]) if target_hit else None

    multiples = bid / entry_ask
    touch_levels = {
        f"touch_{multiple}x": bool((multiples >= multiple).any())
        for multiple in (2, 3, 4)
    }

    max_multiple = max_bid / entry_ask
    min_multiple = min_bid / entry_ask
    mfe_pct = (max_multiple - 1.0) * 100.0
    mae_pct = (min_multiple - 1.0) * 100.0
    end_return_pct = (last_bid / entry_ask - 1.0) * 100.0
    full_loss_proxy = min_bid <= entry_ask * (1.0 - cfg.full_loss_threshold_pct / 100.0)

    return {
        "symbol": candidate.get("symbol"),
        "contract_symbol": candidate.get("contract_symbol"),
        "option_type": candidate.get("option_type"),
        "ranking_score": _finite_or_none(candidate.get("ranking_score")),
        "chart_score": _finite_or_none(candidate.get("chart_score")),
        "option_score": _finite_or_none(candidate.get("option_score") or candidate.get("score")),
        "entry_ask": round(entry_ask, 6),
        "exit_mark": "bid",
        "target_profit_pct": cfg.target_profit_pct,
        "target_multiple": cfg.target_multiple,
        "target_premium": round(target_premium, 6),
        "target_hit": target_hit,
        "target_touch_time": target_touch_time,
        "max_bid": round(max_bid, 6),
        "min_bid": round(min_bid, 6),
        "last_bid": round(last_bid, 6),
        "max_multiple": round(max_multiple, 6),
        "min_multiple": round(min_multiple, 6),
        "mfe_pct": round(mfe_pct, 4),
        "mae_pct": round(mae_pct, 4),
        "end_return_pct": round(end_return_pct, 4),
        "full_loss_proxy": full_loss_proxy,
        "observations": int(len(frame)),
        **touch_levels,
        "label_warning": (
            "Outcome label uses future quotes and is for evaluation only. It must never be used as a point-in-time feature."
        ),
    }


def calibrate_score_buckets(
    outcomes: Iterable[Mapping[str, Any]],
    *,
    score_field: str = "ranking_score",
    config: OutcomeConfig | None = None,
) -> dict[str, Any]:
    """Summarize historical outcome frequencies by score bucket."""
    cfg = config or OutcomeConfig()
    cfg.validate()
    rows = [dict(row) for row in outcomes]
    if not rows:
        return {
            "score_field": score_field,
            "target_profit_pct": cfg.target_profit_pct,
            "buckets": [],
            "warning": "No outcomes supplied; no empirical calibration is available.",
        }

    frame = pd.DataFrame(rows)
    if score_field not in frame.columns:
        raise ValueError(f"outcomes do not contain score field {score_field!r}")
    frame[score_field] = pd.to_numeric(frame[score_field], errors="coerce")
    frame = frame.dropna(subset=[score_field, "target_hit"])
    frame = frame[(frame[score_field] >= 0.0) & (frame[score_field] <= 100.0)]
    if frame.empty:
        return {
            "score_field": score_field,
            "target_profit_pct": cfg.target_profit_pct,
            "buckets": [],
            "warning": "No valid scored outcomes supplied; no empirical calibration is available.",
        }

    bucket_start = (np.floor(frame[score_field] / cfg.score_bucket_size) * cfg.score_bucket_size).clip(upper=100)
    frame["bucket_start"] = bucket_start.astype(int)

    buckets: list[dict[str, Any]] = []
    for start, group in frame.groupby("bucket_start", sort=True):
        end = min(100, int(start) + cfg.score_bucket_size)
        sample_size = int(len(group))
        target_rate = _mean_bool(group["target_hit"])
        full_loss_rate = _mean_bool(group.get("full_loss_proxy", pd.Series(False, index=group.index)))
        touch_2x_rate = _mean_bool(group.get("touch_2x", pd.Series(False, index=group.index)))
        touch_3x_rate = _mean_bool(group.get("touch_3x", pd.Series(False, index=group.index)))
        touch_4x_rate = _mean_bool(group.get("touch_4x", group["target_hit"]))
        max_multiple = pd.to_numeric(group.get("max_multiple"), errors="coerce")
        end_return = pd.to_numeric(group.get("end_return_pct"), errors="coerce")
        buckets.append(
            {
                "score_min": int(start),
                "score_max": end,
                "samples": sample_size,
                "empirical_target_hit_rate": round(target_rate, 4),
                "empirical_2x_touch_rate": round(touch_2x_rate, 4),
                "empirical_3x_touch_rate": round(touch_3x_rate, 4),
                "empirical_4x_touch_rate": round(touch_4x_rate, 4),
                "empirical_full_loss_proxy_rate": round(full_loss_rate, 4),
                "median_max_multiple": _round_or_none(max_multiple.median()),
                "mean_end_return_pct": _round_or_none(end_return.mean()),
                "calibration_valid": sample_size >= cfg.min_bucket_samples,
            }
        )

    return {
        "score_field": score_field,
        "target_profit_pct": cfg.target_profit_pct,
        "target_multiple": cfg.target_multiple,
        "buckets": buckets,
        "config": asdict(cfg),
        "warning": (
            "These are historical frequencies, not guaranteed future probabilities. Use only out-of-sample, point-in-time data "
            "and evaluate stability across walk-forward periods and market regimes before interpreting them predictively."
        ),
    }


def _normalize_quotes(quotes: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(quotes, pd.DataFrame) or "bid" not in quotes.columns:
        return pd.DataFrame(columns=["bid"])
    frame = quotes.copy()
    frame["bid"] = pd.to_numeric(frame["bid"], errors="coerce")
    frame = frame.replace([np.inf, -np.inf], np.nan).dropna(subset=["bid"])
    frame = frame[frame["bid"] >= 0.0]
    if isinstance(frame.index, pd.DatetimeIndex):
        frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    return frame


def _positive(value: object) -> float | None:
    number = _finite_or_none(value)
    if number is None or number <= 0:
        return None
    return number


def _finite_or_none(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _index_value(value: object) -> object:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _mean_bool(series: pd.Series) -> float:
    return float(series.fillna(False).astype(bool).mean())


def _round_or_none(value: object) -> float | None:
    number = _finite_or_none(value)
    return None if number is None else round(number, 4)


__all__ = ["OutcomeConfig", "calibrate_score_buckets", "label_long_option_path"]
