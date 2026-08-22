"""Cross-sectional chart screening for U.S. options research.

The scanner is intentionally independent of the market-data transport. It accepts
normalized OHLCV frames, computes point-in-time features, and ranks the universe
cross-sectionally. This keeps research logic reusable with a slow/free development
feed today and a bulk institutional feed later.

Scores are ranking heuristics, not probabilities or return forecasts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any, Mapping

import numpy as np
import pandas as pd

_REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")


@dataclass(frozen=True)
class ChartScreenConfig:
    """Configuration for the cheap whole-market chart stage."""

    min_bars: int = 220
    min_price: float = 3.0
    min_avg_dollar_volume: float = 20_000_000.0
    min_chart_score: float = 58.0
    min_direction_gap: float = 6.0
    top_n: int = 200

    def validate(self) -> None:
        if self.min_bars < 60:
            raise ValueError("min_bars must be at least 60")
        if self.min_price <= 0:
            raise ValueError("min_price must be positive")
        if self.min_avg_dollar_volume < 0:
            raise ValueError("min_avg_dollar_volume must be non-negative")
        if not 0 <= self.min_chart_score <= 100:
            raise ValueError("min_chart_score must be between 0 and 100")
        if not 0 <= self.min_direction_gap <= 100:
            raise ValueError("min_direction_gap must be between 0 and 100")
        if not 1 <= self.top_n <= 5_000:
            raise ValueError("top_n must be between 1 and 5000")


@dataclass(frozen=True)
class ChartSnapshot:
    """Point-in-time features for one underlying before cross-sectional ranking."""

    symbol: str
    bars: int
    close: float
    avg_dollar_volume_20: float
    return_1d_pct: float
    return_5d_pct: float
    return_20d_pct: float
    return_60d_pct: float
    gap_pct: float
    sma20: float
    sma50: float
    sma200: float
    rsi14: float
    atr14_pct: float
    realized_vol20_pct: float
    relative_volume20: float
    volume_z20: float
    range_position20: float
    breakout20_pct: float
    breakdown20_pct: float
    trend_up: bool
    trend_down: bool


def compute_chart_snapshot(
    symbol: str,
    frame: pd.DataFrame,
    config: ChartScreenConfig | None = None,
) -> ChartSnapshot | None:
    """Compute one symbol's chart features, rejecting low-quality/illiquid data."""
    cfg = config or ChartScreenConfig()
    cfg.validate()
    df = _normalize_frame(frame)
    if len(df) < cfg.min_bars:
        return None

    close = float(df["close"].iloc[-1])
    if not math.isfinite(close) or close < cfg.min_price:
        return None

    dollar_volume = df["close"] * df["volume"]
    avg_dollar_volume_20 = float(dollar_volume.iloc[-20:].mean())
    if not math.isfinite(avg_dollar_volume_20) or avg_dollar_volume_20 < cfg.min_avg_dollar_volume:
        return None

    sma20 = float(df["close"].iloc[-20:].mean())
    sma50 = float(df["close"].iloc[-50:].mean())
    sma200 = float(df["close"].iloc[-200:].mean())

    previous_close = float(df["close"].iloc[-2])
    gap_pct = _pct_change(float(df["open"].iloc[-1]), previous_close)

    prior20 = df.iloc[-21:-1]
    prior_high20 = float(prior20["high"].max())
    prior_low20 = float(prior20["low"].min())
    width20 = prior_high20 - prior_low20
    range_position20 = 0.5 if width20 <= 0 else (close - prior_low20) / width20
    range_position20 = float(np.clip(range_position20, 0.0, 1.0))

    breakout20_pct = _pct_change(close, prior_high20)
    breakdown20_pct = _pct_change(prior_low20, close)

    volume_baseline = df["volume"].iloc[-21:-1]
    volume_mean = float(volume_baseline.mean())
    volume_std = float(volume_baseline.std(ddof=0))
    relative_volume20 = float(df["volume"].iloc[-1] / volume_mean) if volume_mean > 0 else 0.0
    volume_z20 = (
        float((df["volume"].iloc[-1] - volume_mean) / volume_std)
        if volume_std > 0
        else 0.0
    )

    return ChartSnapshot(
        symbol=str(symbol).strip().upper(),
        bars=len(df),
        close=round(close, 6),
        avg_dollar_volume_20=round(avg_dollar_volume_20, 2),
        return_1d_pct=round(_series_return(df["close"], 1), 4),
        return_5d_pct=round(_series_return(df["close"], 5), 4),
        return_20d_pct=round(_series_return(df["close"], 20), 4),
        return_60d_pct=round(_series_return(df["close"], 60), 4),
        gap_pct=round(gap_pct, 4),
        sma20=round(sma20, 6),
        sma50=round(sma50, 6),
        sma200=round(sma200, 6),
        rsi14=round(_rsi(df["close"], 14), 4),
        atr14_pct=round(_atr_pct(df, 14), 4),
        realized_vol20_pct=round(_realized_vol_pct(df["close"], 20), 4),
        relative_volume20=round(relative_volume20, 4),
        volume_z20=round(volume_z20, 4),
        range_position20=round(range_position20, 4),
        breakout20_pct=round(breakout20_pct, 4),
        breakdown20_pct=round(breakdown20_pct, 4),
        trend_up=bool(close > sma20 > sma50 > sma200),
        trend_down=bool(close < sma20 < sma50 < sma200),
    )


def rank_chart_snapshots(
    snapshots: list[ChartSnapshot],
    config: ChartScreenConfig | None = None,
) -> list[dict[str, Any]]:
    """Rank chart snapshots cross-sectionally for bullish and bearish setups."""
    cfg = config or ChartScreenConfig()
    cfg.validate()
    if not snapshots:
        return []

    rows = [asdict(snapshot) for snapshot in snapshots]
    frame = pd.DataFrame(rows)

    ret5_pct = _percentile(frame["return_5d_pct"])
    ret20_pct = _percentile(frame["return_20d_pct"])
    relvol_pct = _percentile(frame["relative_volume20"])
    atr_pct = _percentile(frame["atr14_pct"])
    liquidity_pct = _percentile(np.log10(frame["avg_dollar_volume_20"].clip(lower=1.0)))

    bull_momentum = 0.35 * ret5_pct + 0.65 * ret20_pct
    bear_momentum = 1.0 - bull_momentum

    close = frame["close"]
    sma20 = frame["sma20"]
    sma50 = frame["sma50"]
    sma200 = frame["sma200"]
    bull_trend = (
        0.25 * (close > sma20).astype(float)
        + 0.35 * (sma20 > sma50).astype(float)
        + 0.40 * (sma50 > sma200).astype(float)
    )
    bear_trend = (
        0.25 * (close < sma20).astype(float)
        + 0.35 * (sma20 < sma50).astype(float)
        + 0.40 * (sma50 < sma200).astype(float)
    )

    range_position = frame["range_position20"].clip(lower=0.0, upper=1.0)
    movement_fit = 1.0 - (atr_pct - 0.75).abs() / 0.75
    movement_fit = movement_fit.clip(lower=0.0, upper=1.0)

    bull_score = 100.0 * (
        0.28 * bull_momentum
        + 0.22 * bull_trend
        + 0.16 * range_position
        + 0.12 * relvol_pct
        + 0.12 * movement_fit
        + 0.10 * liquidity_pct
    )
    bear_score = 100.0 * (
        0.28 * bear_momentum
        + 0.22 * bear_trend
        + 0.16 * (1.0 - range_position)
        + 0.12 * relvol_pct
        + 0.12 * movement_fit
        + 0.10 * liquidity_pct
    )

    frame["bull_score"] = bull_score.round(2)
    frame["bear_score"] = bear_score.round(2)
    frame["direction"] = np.where(frame["bull_score"] >= frame["bear_score"], "bullish", "bearish")
    frame["chart_score"] = frame[["bull_score", "bear_score"]].max(axis=1)
    frame["direction_gap"] = (frame["bull_score"] - frame["bear_score"]).abs()

    frame = frame[
        (frame["chart_score"] >= cfg.min_chart_score)
        & (frame["direction_gap"] >= cfg.min_direction_gap)
    ].copy()
    if frame.empty:
        return []

    frame["option_type"] = np.where(frame["direction"] == "bullish", "call", "put")
    frame["setup_type"] = frame.apply(_setup_type, axis=1)
    frame = frame.sort_values(
        ["chart_score", "direction_gap", "avg_dollar_volume_20"],
        ascending=[False, False, False],
        kind="mergesort",
    ).head(cfg.top_n)
    frame["rank"] = np.arange(1, len(frame) + 1)

    output = frame.to_dict(orient="records")
    for row in output:
        row["chart_score"] = round(float(row["chart_score"]), 2)
        row["direction_gap"] = round(float(row["direction_gap"]), 2)
        row["score_interpretation"] = "cross-sectional ranking score; not a probability or return forecast"
    return output


def scan_market_frames(
    frames: Mapping[str, pd.DataFrame],
    config: ChartScreenConfig | None = None,
) -> dict[str, Any]:
    """Run the whole-market chart stage over an arbitrary symbol->OHLCV mapping."""
    cfg = config or ChartScreenConfig()
    cfg.validate()
    snapshots: list[ChartSnapshot] = []
    rejected = 0
    for symbol, frame in frames.items():
        snapshot = compute_chart_snapshot(symbol, frame, cfg)
        if snapshot is None:
            rejected += 1
            continue
        snapshots.append(snapshot)

    candidates = rank_chart_snapshots(snapshots, cfg)
    return {
        "mode": "us_options_chart_screen",
        "universe_received": len(frames),
        "eligible_for_cross_section": len(snapshots),
        "rejected_before_ranking": rejected,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "config": asdict(cfg),
        "warning": (
            "Research screen only. The ranking compares chart structure and liquidity across the supplied universe; "
            "it is not a probability estimate, expected return, trade instruction, or guarantee."
        ),
    }


def _normalize_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return pd.DataFrame(columns=_REQUIRED_COLUMNS)
    missing = [column for column in _REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        return pd.DataFrame(columns=_REQUIRED_COLUMNS)
    df = frame.loc[:, list(_REQUIRED_COLUMNS)].copy()
    for column in _REQUIRED_COLUMNS:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df = df.replace([np.inf, -np.inf], np.nan).dropna(subset=["open", "high", "low", "close"])
    df["volume"] = df["volume"].fillna(0.0).clip(lower=0.0)
    df = df[(df["open"] > 0) & (df["high"] > 0) & (df["low"] > 0) & (df["close"] > 0)]
    if isinstance(df.index, pd.DatetimeIndex):
        df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


def _series_return(series: pd.Series, lookback: int) -> float:
    if len(series) <= lookback:
        return 0.0
    return _pct_change(float(series.iloc[-1]), float(series.iloc[-1 - lookback]))


def _pct_change(value: float, base: float) -> float:
    if not math.isfinite(value) or not math.isfinite(base) or base == 0:
        return 0.0
    return (value / base - 1.0) * 100.0


def _rsi(close: pd.Series, period: int) -> float:
    delta = close.diff().iloc[-period:]
    gains = delta.clip(lower=0.0).mean()
    losses = -delta.clip(upper=0.0).mean()
    if losses <= 0:
        return 100.0 if gains > 0 else 50.0
    rs = gains / losses
    return float(100.0 - 100.0 / (1.0 + rs))


def _atr_pct(frame: pd.DataFrame, period: int) -> float:
    high = frame["high"]
    low = frame["low"]
    previous_close = frame["close"].shift(1)
    true_range = pd.concat(
        [(high - low).abs(), (high - previous_close).abs(), (low - previous_close).abs()],
        axis=1,
    ).max(axis=1)
    atr = float(true_range.iloc[-period:].mean())
    close = float(frame["close"].iloc[-1])
    return atr / close * 100.0 if close > 0 else 0.0


def _realized_vol_pct(close: pd.Series, period: int) -> float:
    returns = np.log(close / close.shift(1)).replace([np.inf, -np.inf], np.nan).dropna().iloc[-period:]
    if len(returns) < 2:
        return 0.0
    return float(returns.std(ddof=0) * math.sqrt(252.0) * 100.0)


def _percentile(values: pd.Series) -> pd.Series:
    numeric = pd.Series(values, index=values.index, dtype=float)
    if numeric.nunique(dropna=True) <= 1:
        return pd.Series(0.5, index=numeric.index, dtype=float)
    return numeric.rank(method="average", pct=True).fillna(0.5)


def _setup_type(row: pd.Series) -> str:
    direction = str(row["direction"])
    relative_volume = float(row["relative_volume20"])
    if direction == "bullish":
        if float(row["breakout20_pct"]) >= 0.0 and relative_volume >= 1.2:
            return "breakout"
        if bool(row["trend_up"]):
            return "trend_momentum"
        return "bullish_momentum"
    if float(row["breakdown20_pct"]) >= 0.0 and relative_volume >= 1.2:
        return "breakdown"
    if bool(row["trend_down"]):
        return "trend_momentum"
    return "bearish_momentum"


__all__ = [
    "ChartScreenConfig",
    "ChartSnapshot",
    "compute_chart_snapshot",
    "rank_chart_snapshots",
    "scan_market_frames",
]
