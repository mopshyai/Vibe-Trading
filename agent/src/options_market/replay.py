"""Point-in-time replay of the market -> chart -> option selection pipeline.

Selection reads only observations whose ``available_at`` is at or before the
research timestamp. Future option quotes are accessed only by the separate
labeling function, which makes the anti-lookahead boundary explicit in code.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import math
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd

from .chart_screen import ChartScreenConfig, scan_market_frames
from .outcomes import OutcomeConfig, label_long_option_path
from .pipeline import OptionsMarketPipelineConfig, combine_rankings
from .store import OptionsResearchStore


@dataclass(frozen=True)
class ReplayConfig:
    history_days: int = 420
    chart_top_n: int = 200
    max_deep_symbols: int = 50
    final_top_n: int = 10
    min_dte: int = 7
    max_dte: int = 60
    target_profit_pct: float = 300.0
    quote_lookback_minutes: int = 30
    max_spread_pct: float = 20.0
    min_open_interest_when_available: int = 100
    max_required_move_vs_one_sigma: float = 1.75

    def validate(self) -> None:
        if self.history_days < 220:
            raise ValueError("history_days must be at least 220")
        if not 1 <= self.max_deep_symbols <= self.chart_top_n:
            raise ValueError("max_deep_symbols must be <= chart_top_n")
        if not 1 <= self.final_top_n <= self.max_deep_symbols:
            raise ValueError("final_top_n must be <= max_deep_symbols")
        if self.min_dte < 1 or self.max_dte < self.min_dte:
            raise ValueError("invalid DTE range")
        if self.target_profit_pct <= 0:
            raise ValueError("target_profit_pct must be positive")
        if self.quote_lookback_minutes < 1:
            raise ValueError("quote_lookback_minutes must be positive")
        if not 0 < self.max_spread_pct <= 100:
            raise ValueError("max_spread_pct must be > 0 and <= 100")
        if self.min_open_interest_when_available < 0:
            raise ValueError("min_open_interest_when_available cannot be negative")
        if self.max_required_move_vs_one_sigma <= 0:
            raise ValueError("max_required_move_vs_one_sigma must be positive")


def replay_selection_at(
    store: OptionsResearchStore,
    symbols: Sequence[str],
    *,
    research_time: datetime,
    config: ReplayConfig | None = None,
) -> dict[str, Any]:
    """Recreate what the pipeline could have selected at ``research_time``."""
    cfg = config or ReplayConfig()
    cfg.validate()
    as_of = _aware(research_time, "research_time")
    clean_symbols = _symbols(symbols)
    frames = store.equity_frames_asof(
        clean_symbols,
        start=as_of - timedelta(days=cfg.history_days),
        end=as_of,
        as_of=as_of,
        interval="1D",
    )
    chart_stage = scan_market_frames(frames, ChartScreenConfig(top_n=cfg.chart_top_n))
    chart_candidates = [dict(row) for row in chart_stage.get("candidates", []) if isinstance(row, Mapping)]
    deep = chart_candidates[: cfg.max_deep_symbols]
    deep_symbols = _symbols(row.get("symbol") for row in deep)

    raw_quotes = store.option_quotes_asof(
        as_of=as_of,
        start=as_of - timedelta(minutes=cfg.quote_lookback_minutes),
        end=as_of,
        underlyings=deep_symbols,
    )
    latest = _latest_contract_quotes(raw_quotes)
    spot_by_symbol = {
        symbol: float(frame["close"].iloc[-1])
        for symbol, frame in frames.items()
        if isinstance(frame, pd.DataFrame) and not frame.empty and "close" in frame.columns
    }

    options_by_symbol: dict[str, list[dict[str, Any]]] = {}
    completeness_warnings: set[str] = set()
    for chart in deep:
        symbol = str(chart.get("symbol") or "").upper()
        direction = str(chart.get("direction") or "").lower()
        option_type = "call" if direction == "bullish" else "put" if direction == "bearish" else ""
        spot = spot_by_symbol.get(symbol)
        if not option_type or spot is None or spot <= 0:
            continue
        rows = latest[latest["underlying"].astype(str).str.upper() == symbol] if not latest.empty else latest
        candidates: list[dict[str, Any]] = []
        for _, row in rows.iterrows():
            if str(row.get("option_type") or "").lower() != option_type:
                continue
            candidate, warnings = _score_store_quote(row, spot=spot, as_of=as_of, config=cfg)
            completeness_warnings.update(warnings)
            if candidate is not None:
                candidates.append(candidate)
        candidates.sort(
            key=lambda item: (
                -float(item["score"]),
                float(item["required_move_vs_one_sigma"]),
                float(item["spread_pct"]),
            )
        )
        options_by_symbol[symbol] = candidates[:25]

    final_stage = combine_rankings(
        deep,
        options_by_symbol,
        config=OptionsMarketPipelineConfig(
            target_profit_pct=cfg.target_profit_pct,
            max_required_move_vs_one_sigma=cfg.max_required_move_vs_one_sigma,
            final_top_n=cfg.final_top_n,
        ),
    )
    for row in final_stage.get("candidates", []):
        if isinstance(row, dict):
            row["entry_time"] = as_of.isoformat()

    return {
        "mode": "point_in_time_options_replay_selection",
        "research_time": as_of.isoformat(),
        "universe_requested": len(clean_symbols),
        "equity_frames_available": len(frames),
        "option_quotes_in_lookback": int(len(raw_quotes)),
        "latest_option_contracts": int(len(latest)),
        "chart_stage": chart_stage,
        "final_stage": final_stage,
        "data_completeness_warnings": sorted(completeness_warnings),
        "config": asdict(cfg),
        "anti_lookahead": "selection used only rows available_at <= research_time",
    }


def label_replay_candidates(
    store: OptionsResearchStore,
    candidates: Iterable[Mapping[str, Any]],
    *,
    evaluation_as_of: datetime,
    target_profit_pct: float = 300.0,
) -> list[dict[str, Any]]:
    """Label replay selections using future quotes, strictly after selection."""
    evaluation = _aware(evaluation_as_of, "evaluation_as_of")
    outcome_cfg = OutcomeConfig(target_profit_pct=target_profit_pct)
    outcomes: list[dict[str, Any]] = []
    for raw in candidates:
        candidate = dict(raw)
        contract = str(candidate.get("contract_symbol") or "").strip().upper()
        entry_time = _parse_time(candidate.get("entry_time"))
        if not contract or entry_time is None or evaluation <= entry_time:
            continue
        expiration = _parse_time(candidate.get("expiration"))
        end = min(evaluation, expiration) if expiration is not None else evaluation
        quotes = store.option_quotes_asof(
            as_of=evaluation,
            start=entry_time,
            end=end,
            contracts=[contract],
        )
        if quotes.empty:
            continue
        frame = quotes.sort_values("event_ts").set_index(pd.DatetimeIndex(pd.to_datetime(quotes["event_ts"], utc=True)))
        try:
            outcome = label_long_option_path(candidate, frame[["bid"]], config=outcome_cfg)
        except ValueError:
            continue
        outcome["entry_time"] = entry_time.isoformat()
        outcome["evaluation_as_of"] = evaluation.isoformat()
        outcomes.append(outcome)
    return outcomes


def replay_many(
    store: OptionsResearchStore,
    symbols: Sequence[str],
    research_times: Iterable[datetime],
    *,
    evaluation_as_of: datetime | None = None,
    config: ReplayConfig | None = None,
) -> dict[str, Any]:
    """Run several point-in-time selections and optionally label them later."""
    cfg = config or ReplayConfig()
    selections = [
        replay_selection_at(store, symbols, research_time=timestamp, config=cfg)
        for timestamp in research_times
    ]
    selected_candidates: list[dict[str, Any]] = []
    for result in selections:
        selected_candidates.extend(
            dict(row)
            for row in result.get("final_stage", {}).get("candidates", [])
            if isinstance(row, Mapping)
        )
    outcomes = None
    if evaluation_as_of is not None:
        outcomes = label_replay_candidates(
            store,
            selected_candidates,
            evaluation_as_of=evaluation_as_of,
            target_profit_pct=cfg.target_profit_pct,
        )
    return {
        "mode": "point_in_time_options_replay",
        "selection_count": len(selections),
        "selected_candidate_count": len(selected_candidates),
        "labeled_outcome_count": None if outcomes is None else len(outcomes),
        "selections": selections,
        "outcomes": outcomes,
        "config": asdict(cfg),
        "warning": (
            "Selection and outcome labeling are intentionally separated. Future quotes belong only to outcomes and "
            "must never be joined back into point-in-time features."
        ),
    }


def _score_store_quote(
    row: Mapping[str, Any],
    *,
    spot: float,
    as_of: datetime,
    config: ReplayConfig,
) -> tuple[dict[str, Any] | None, list[str]]:
    warnings: list[str] = []
    strike = _positive(row.get("strike"))
    bid = _nonnegative(row.get("bid"))
    ask = _positive(row.get("ask"))
    iv = _positive(row.get("implied_volatility"))
    expiration = _parse_time(row.get("expiration"))
    if strike is None or bid is None or ask is None or ask < bid or expiration is None:
        return None, warnings
    if iv is None:
        # Historical OPRA CBBO from the current Databento adapter does not carry
        # implied volatility. Reject the contract, but make the data limitation
        # explicit so the session cannot be misread as an ordinary strategy
        # NO_TRADE decision.
        warnings.append("historical_implied_volatility_missing")
        return None, warnings
    dte = max(0, int(math.ceil((expiration - as_of).total_seconds() / 86400.0)))
    if not config.min_dte <= dte <= config.max_dte:
        return None, warnings

    midpoint = (bid + ask) / 2.0
    if midpoint <= 0:
        return None, warnings
    spread_pct = (ask - bid) / midpoint * 100.0
    if spread_pct > config.max_spread_pct:
        return None, warnings

    oi = _integer_or_none(row.get("open_interest"))
    volume = _integer_or_none(row.get("volume"))
    if oi is None:
        warnings.append("historical_open_interest_missing")
        oi_for_score = 0
    else:
        if oi < config.min_open_interest_when_available:
            return None, warnings
        oi_for_score = oi
    if volume is None:
        warnings.append("historical_option_volume_missing")

    option_type = str(row.get("option_type") or "").lower()
    multiple = 1.0 + config.target_profit_pct / 100.0
    target_premium = ask * multiple
    if option_type == "call":
        target_underlying = strike + target_premium
        required_move = max(0.0, (target_underlying - spot) / spot * 100.0)
    elif option_type == "put":
        target_underlying = strike - target_premium
        if target_underlying <= 0:
            return None, warnings
        required_move = max(0.0, (spot - target_underlying) / spot * 100.0)
    else:
        return None, warnings
    one_sigma = iv * math.sqrt(dte / 365.0) * 100.0
    if one_sigma <= 0:
        return None, warnings
    move_ratio = required_move / one_sigma
    if move_ratio > config.max_required_move_vs_one_sigma:
        return None, warnings

    spread_score = 30.0 * max(0.0, 1.0 - spread_pct / config.max_spread_pct)
    oi_score = 20.0 * min(1.0, math.log10(oi_for_score + 1.0) / 4.0) if oi_for_score > 0 else 0.0
    move_score = 40.0 * max(0.0, 1.0 - move_ratio / config.max_required_move_vs_one_sigma)
    dte_score = 10.0 * max(0.0, 1.0 - abs(dte - 30.0) / 30.0)
    completeness_bonus = 0.0 if warnings else 5.0
    score = min(100.0, spread_score + oi_score + move_score + dte_score + completeness_bonus)

    return {
        "ticker": str(row.get("underlying") or "").upper(),
        "contract_symbol": row.get("contract_symbol"),
        "option_type": option_type,
        "strike": round(strike, 4),
        "expiration": expiration.isoformat(),
        "dte": dte,
        "spot": round(spot, 4),
        "bid": round(bid, 4),
        "entry_ask": round(ask, 4),
        "spread_pct": round(spread_pct, 4),
        "open_interest": oi,
        "volume": volume,
        "implied_volatility": round(iv, 8),
        "max_loss_usd": round(ask * 100.0, 2),
        "target_profit_pct": config.target_profit_pct,
        "target_multiple": round(multiple, 4),
        "target_premium": round(target_premium, 4),
        "target_underlying_at_expiry": round(target_underlying, 4),
        "required_underlying_move_pct": round(required_move, 4),
        "one_sigma_implied_move_pct": round(one_sigma, 4),
        "required_move_vs_one_sigma": round(move_ratio, 4),
        "score": round(score, 2),
        "data_completeness_warnings": warnings,
    }, warnings


def _latest_contract_quotes(frame: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame) or frame.empty or "contract_symbol" not in frame.columns:
        return pd.DataFrame(columns=getattr(frame, "columns", []))
    clean = frame.copy()
    clean["event_ts"] = pd.to_datetime(clean["event_ts"], utc=True, errors="coerce")
    clean = clean.dropna(subset=["event_ts", "contract_symbol"]).sort_values("event_ts")
    return clean.groupby("contract_symbol", sort=False, as_index=False).tail(1).reset_index(drop=True)


def _symbols(values: Iterable[object]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        symbol = str(value or "").strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            output.append(symbol)
    return output


def _aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _parse_time(value: object) -> datetime | None:
    if value is None:
        return None
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        return None
    return pd.Timestamp(parsed).to_pydatetime()


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


def _integer_or_none(value: object) -> int | None:
    number = _finite(value)
    if number is None or int(number) != number:
        return None
    return int(number)


__all__ = ["ReplayConfig", "label_replay_candidates", "replay_many", "replay_selection_at"]
