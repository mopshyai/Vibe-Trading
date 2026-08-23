"""Reproducible point-in-time historical experiments for options research.

This module orchestrates the existing replay primitives across many historical
research timestamps while keeping selection and later outcome labels separated.
It is deliberately strict about universe provenance: a static current-day symbol
list can be used only when survivorship bias is explicitly acknowledged.

Experiments are research/evaluation only. They cannot submit broker orders and
do not automatically change strategy or risk policy.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from statistics import median
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd

from .outcomes import OutcomeConfig, label_long_option_path
from .replay import ReplayConfig, replay_selection_at
from .store import OptionsResearchStore
from .surface import analyze_volatility_surface, contract_surface_context

UTC = timezone.utc


@dataclass(frozen=True)
class HistoricalExperimentConfig:
    outcome_horizon_days: int = 30
    minimum_quote_observations: int = 2
    max_sessions: int = 1_000
    allow_static_universe: bool = False
    min_bucket_samples: int = 30

    def validate(self) -> None:
        if not 1 <= self.outcome_horizon_days <= 365:
            raise ValueError("outcome_horizon_days must be between 1 and 365")
        if self.minimum_quote_observations < 1:
            raise ValueError("minimum_quote_observations must be at least 1")
        if not 1 <= self.max_sessions <= 10_000:
            raise ValueError("max_sessions must be between 1 and 10000")
        if self.min_bucket_samples < 1:
            raise ValueError("min_bucket_samples must be at least 1")


@dataclass(frozen=True)
class ExperimentLineage:
    experiment_version: str = "historical-research-replay-v0.1"
    strategy_version: str = "personal-options-v0.1"
    model_version: str = "rules-and-empirical-v0.1"
    risk_policy_version: str = "personal-risk-v0.1"
    payoff_policy_version: str = "adaptive-payoff-v0.1"
    commit_sha: str | None = None


def run_historical_research_experiment(
    store: OptionsResearchStore,
    research_times: Iterable[datetime],
    *,
    evaluation_as_of: datetime,
    static_symbols: Sequence[str] | None = None,
    universe_snapshots: Sequence[Mapping[str, Any]] | None = None,
    replay_config: ReplayConfig | None = None,
    experiment_config: HistoricalExperimentConfig | None = None,
    lineage: ExperimentLineage | None = None,
) -> dict[str, Any]:
    """Replay many historical selections and label only mature fixed horizons.

    ``universe_snapshots`` entries must contain ``available_at`` and ``symbols``.
    For each research timestamp the latest snapshot with ``available_at <= time``
    is used. A static symbol list is rejected unless ``allow_static_universe`` is
    explicitly enabled because it can introduce survivorship bias.
    """
    cfg = experiment_config or HistoricalExperimentConfig()
    cfg.validate()
    replay_cfg = replay_config or ReplayConfig()
    replay_cfg.validate()
    identity = lineage or ExperimentLineage()
    evaluation = _aware(evaluation_as_of, "evaluation_as_of")
    times = _research_times(research_times, evaluation=evaluation)
    if not times:
        raise ValueError("at least one historical research timestamp is required")
    if len(times) > cfg.max_sessions:
        raise ValueError(f"research timestamp count exceeds max_sessions={cfg.max_sessions}")

    resolver = _UniverseResolver(
        static_symbols=static_symbols,
        snapshots=universe_snapshots,
        allow_static=cfg.allow_static_universe,
    )
    experiment_id = _experiment_id(
        times=times,
        evaluation=evaluation,
        replay_config=replay_cfg,
        experiment_config=cfg,
        lineage=identity,
        universe_mode=resolver.mode,
    )

    sessions: list[dict[str, Any]] = []
    selected: list[dict[str, Any]] = []
    skipped_sessions: list[dict[str, str]] = []
    completeness_warnings: set[str] = set(resolver.warnings)

    for research_time in times:
        universe, universe_meta = resolver.resolve(research_time)
        if not universe:
            skipped_sessions.append(
                {
                    "research_time": research_time.isoformat(),
                    "reason": "point_in_time_universe_unavailable",
                }
            )
            continue
        replay = replay_selection_at(
            store,
            universe,
            research_time=research_time,
            config=replay_cfg,
        )
        session_candidates = [
            dict(row)
            for row in _mapping(replay.get("final_stage")).get("candidates", [])
            if isinstance(row, Mapping)
        ]
        surface_warnings: set[str] = set()
        enriched = _enrich_historical_surfaces(
            store,
            session_candidates,
            research_time=research_time,
            surface_warnings=surface_warnings,
        )
        for candidate in enriched:
            candidate["research_time"] = research_time.isoformat()
            candidate["universe_mode"] = resolver.mode
            candidate["universe_snapshot_available_at"] = universe_meta.get("available_at")
            candidate["experiment_id"] = experiment_id
        selected.extend(enriched)
        replay_warnings = [str(item) for item in replay.get("data_completeness_warnings", [])]
        completeness_warnings.update(replay_warnings)
        completeness_warnings.update(surface_warnings)
        sessions.append(
            {
                "research_time": research_time.isoformat(),
                "universe_size": len(universe),
                "universe": universe_meta,
                "equity_frames_available": replay.get("equity_frames_available"),
                "option_quotes_in_lookback": replay.get("option_quotes_in_lookback"),
                "chart_candidates": _mapping(replay.get("chart_stage")).get("candidate_count", 0),
                "selected_candidates": len(enriched),
                "decision": _mapping(replay.get("final_stage")).get("decision", "NO_TRADE"),
                "warnings": sorted(set(replay_warnings) | surface_warnings),
            }
        )

    outcomes, immature, skipped_outcomes = _label_mature_candidates(
        store,
        selected,
        evaluation=evaluation,
        replay_config=replay_cfg,
        experiment_config=cfg,
    )
    report = summarize_historical_experiment(
        outcomes,
        min_bucket_samples=cfg.min_bucket_samples,
    )

    return {
        "mode": "point_in_time_historical_research_experiment",
        "experiment_id": experiment_id,
        "created_as_of": evaluation.isoformat(),
        "lineage": asdict(identity),
        "universe_mode": resolver.mode,
        "survivorship_bias_control": {
            "point_in_time_universe": resolver.mode == "point_in_time_snapshots",
            "static_universe_explicitly_allowed": resolver.mode == "static_explicitly_allowed",
            "warning": (
                None
                if resolver.mode == "point_in_time_snapshots"
                else "Static universe mode can contain survivorship bias and must not be represented as survivorship-safe."
            ),
        },
        "session_count_requested": len(times),
        "session_count_completed": len(sessions),
        "session_count_skipped": len(skipped_sessions),
        "sessions_with_no_trade": sum(str(row.get("decision")) == "NO_TRADE" for row in sessions),
        "selected_candidate_count": len(selected),
        "mature_outcome_count": len(outcomes),
        "immature_candidate_count": immature,
        "skipped_outcome_count": len(skipped_outcomes),
        "sessions": sessions,
        "skipped_sessions": skipped_sessions,
        "selected_candidates": selected,
        "outcomes": outcomes,
        "skipped_outcomes": skipped_outcomes,
        "summary": report,
        "data_completeness_warnings": sorted(completeness_warnings),
        "replay_config": asdict(replay_cfg),
        "experiment_config": asdict(cfg),
        "anti_lookahead": (
            "Every selection uses available_at <= research_time. Outcomes are labeled only after the fixed horizon has matured and are never feature inputs."
        ),
        "warning": (
            "Historical research experiment only. Results are not guaranteed future probabilities and remain sensitive to universe history, data gaps, spreads, slippage and regime stability."
        ),
    }


def summarize_historical_experiment(
    outcomes: Iterable[Mapping[str, Any]],
    *,
    min_bucket_samples: int = 30,
) -> dict[str, Any]:
    """Summarize fixed-horizon outcomes across selection and surface dimensions."""
    if min_bucket_samples < 1:
        raise ValueError("min_bucket_samples must be at least 1")
    rows = [dict(row) for row in outcomes]
    dimensions: dict[str, dict[str, list[dict[str, Any]]]] = {
        "direction": {},
        "setup_type": {},
        "final_rank": {},
        "ranking_score": {},
        "required_move_vs_one_sigma": {},
        "surface_efficiency": {},
        "surface_required_move": {},
        "surface_term_structure": {},
        "surface_skew": {},
        "surface_implied_vs_realized": {},
    }
    for row in rows:
        keys = {
            "direction": _text(row.get("direction")),
            "setup_type": _text(row.get("setup_type")),
            "final_rank": _rank_bucket(row.get("final_rank")),
            "ranking_score": _score_bucket(row.get("ranking_score")),
            "required_move_vs_one_sigma": _ratio_bucket(row.get("required_move_vs_one_sigma")),
            "surface_efficiency": _surface_efficiency_bucket(row.get("surface_efficiency_score")),
            "surface_required_move": _surface_move_bucket(row.get("surface_required_move_ratio")),
            "surface_term_structure": _text(row.get("surface_term_structure_state")),
            "surface_skew": _text(row.get("surface_skew_state")),
            "surface_implied_vs_realized": _text(row.get("surface_implied_vs_realized_state")),
        }
        for dimension, key in keys.items():
            if key:
                dimensions[dimension].setdefault(key, []).append(row)

    return {
        "samples": len(rows),
        "overall": _outcome_stats(rows, min_bucket_samples=min_bucket_samples),
        "dimensions": {
            dimension: [
                {
                    "bucket": bucket,
                    **_outcome_stats(group, min_bucket_samples=min_bucket_samples),
                }
                for bucket, group in sorted(groups.items())
            ]
            for dimension, groups in dimensions.items()
        },
        "min_bucket_samples": min_bucket_samples,
        "warning": "Bucket statistics are descriptive until validated out-of-sample with sufficient samples.",
    }


class _UniverseResolver:
    def __init__(
        self,
        *,
        static_symbols: Sequence[str] | None,
        snapshots: Sequence[Mapping[str, Any]] | None,
        allow_static: bool,
    ) -> None:
        self._static = _symbols(static_symbols or [])
        self._snapshots: list[tuple[datetime, list[str], str | None]] = []
        self.warnings: list[str] = []
        for index, raw in enumerate(snapshots or []):
            available_at = _timestamp(raw.get("available_at"))
            symbols = _symbols(raw.get("symbols") if isinstance(raw.get("symbols"), Sequence) and not isinstance(raw.get("symbols"), (str, bytes)) else [])
            if available_at is None or not symbols:
                raise ValueError(f"universe snapshot {index} requires timezone-aware available_at and non-empty symbols")
            self._snapshots.append((available_at, symbols, _text(raw.get("source"))))
        self._snapshots.sort(key=lambda item: item[0])

        if self._snapshots:
            if self._static:
                raise ValueError("provide point-in-time universe snapshots or static_symbols, not both")
            self.mode = "point_in_time_snapshots"
        elif self._static:
            if not allow_static:
                raise ValueError(
                    "static_symbols can introduce survivorship bias; set allow_static_universe=True only for explicitly biased exploratory research"
                )
            self.mode = "static_explicitly_allowed"
            self.warnings.append("static_universe_survivorship_bias_possible")
        else:
            raise ValueError("point-in-time universe snapshots are required unless an explicitly allowed static universe is supplied")

    def resolve(self, research_time: datetime) -> tuple[list[str], dict[str, Any]]:
        if self.mode == "static_explicitly_allowed":
            return list(self._static), {
                "mode": self.mode,
                "available_at": None,
                "source": "explicit_static_symbols",
                "symbols": len(self._static),
            }
        eligible = [item for item in self._snapshots if item[0] <= research_time]
        if not eligible:
            return [], {"mode": self.mode, "available_at": None, "source": None, "symbols": 0}
        available_at, symbols, source = eligible[-1]
        return list(symbols), {
            "mode": self.mode,
            "available_at": available_at.isoformat(),
            "source": source,
            "symbols": len(symbols),
        }


def _enrich_historical_surfaces(
    store: OptionsResearchStore,
    candidates: list[dict[str, Any]],
    *,
    research_time: datetime,
    surface_warnings: set[str],
) -> list[dict[str, Any]]:
    if not candidates:
        return []
    underlyings = _symbols(row.get("symbol") or row.get("ticker") for row in candidates)
    quotes = store.option_quotes_asof(
        as_of=research_time,
        start=research_time - timedelta(minutes=30),
        end=research_time,
        underlyings=underlyings,
    )
    if quotes.empty:
        surface_warnings.add("historical_surface_quotes_unavailable")
        return candidates
    quotes = _latest_contract_quotes(quotes)
    output: list[dict[str, Any]] = []
    for raw in candidates:
        candidate = dict(raw)
        symbol = str(candidate.get("symbol") or candidate.get("ticker") or "").strip().upper()
        spot = _positive(candidate.get("spot"))
        if not symbol or spot is None:
            output.append(candidate)
            continue
        group = quotes[quotes["underlying"].astype(str).str.upper() == symbol]
        surface_rows: list[dict[str, Any]] = []
        for _, row in group.iterrows():
            expiration = _timestamp(row.get("expiration"))
            if expiration is None:
                continue
            dte = max(0, int(math.ceil((expiration - research_time).total_seconds() / 86400.0)))
            if dte <= 0:
                continue
            surface_rows.append(
                {
                    "contract_symbol": row.get("contract_symbol"),
                    "option_type": row.get("option_type"),
                    "strike": row.get("strike"),
                    "expiration": expiration.date().isoformat(),
                    "dte": dte,
                    "bid": row.get("bid"),
                    "ask": row.get("ask"),
                    "implied_volatility": row.get("implied_volatility"),
                    "open_interest": row.get("open_interest"),
                    # Historical quote schema does not currently preserve Greeks,
                    # so 25-delta skew will remain explicitly unavailable here.
                    "delta": None,
                }
            )
        try:
            surface = analyze_volatility_surface(
                surface_rows,
                spot=spot,
                realized_vol_pct=_positive(candidate.get("realized_vol20_pct")),
            )
        except ValueError:
            surface_warnings.add("historical_surface_invalid")
            output.append(candidate)
            continue
        surface_warnings.update(str(item) for item in surface.get("warnings", []))
        context = contract_surface_context(candidate, surface)
        candidate["surface_context"] = context
        candidate["surface_efficiency_score"] = context.get("surface_efficiency_score")
        candidate["surface_iv_percentile"] = context.get("surface_iv_percentile")
        candidate["surface_required_move_ratio"] = context.get("required_move_vs_surface_expected_move")
        candidate["surface_term_structure_state"] = context.get("term_structure_state")
        candidate["surface_skew_state"] = context.get("skew_state")
        candidate["surface_implied_vs_realized_state"] = context.get("implied_vs_realized_state")
        output.append(candidate)
    return output


def _label_mature_candidates(
    store: OptionsResearchStore,
    candidates: list[dict[str, Any]],
    *,
    evaluation: datetime,
    replay_config: ReplayConfig,
    experiment_config: HistoricalExperimentConfig,
) -> tuple[list[dict[str, Any]], int, list[dict[str, str]]]:
    outcomes: list[dict[str, Any]] = []
    immature = 0
    skipped: list[dict[str, str]] = []
    outcome_cfg = OutcomeConfig(target_profit_pct=replay_config.target_profit_pct)
    for candidate in candidates:
        contract = str(candidate.get("contract_symbol") or "").strip().upper()
        research_time = _timestamp(candidate.get("research_time"))
        if not contract or research_time is None:
            skipped.append({"contract_symbol": contract, "reason": "selection_timestamp_missing"})
            continue
        expiration = _timestamp(candidate.get("expiration"))
        horizon_end = research_time + timedelta(days=experiment_config.outcome_horizon_days)
        evaluation_end = min(horizon_end, expiration) if expiration is not None else horizon_end
        if evaluation < evaluation_end:
            immature += 1
            continue
        quotes = store.option_quotes_asof(
            as_of=evaluation,
            start=research_time,
            end=evaluation_end,
            contracts=[contract],
        )
        if quotes.empty or len(quotes) < experiment_config.minimum_quote_observations:
            skipped.append({"contract_symbol": contract, "reason": "insufficient_fixed_horizon_quotes"})
            continue
        frame = quotes.copy()
        frame["event_ts"] = pd.to_datetime(frame["event_ts"], utc=True, errors="coerce")
        frame = frame.dropna(subset=["event_ts"]).sort_values("event_ts")
        if len(frame) < experiment_config.minimum_quote_observations:
            skipped.append({"contract_symbol": contract, "reason": "insufficient_fixed_horizon_quotes"})
            continue
        frame = frame.set_index("event_ts")
        try:
            labeled = label_long_option_path(candidate, frame[["bid"]], config=outcome_cfg)
        except ValueError:
            skipped.append({"contract_symbol": contract, "reason": "outcome_label_error"})
            continue
        for field in (
            "research_time",
            "direction",
            "setup_type",
            "final_rank",
            "ranking_score",
            "option_score",
            "required_move_vs_one_sigma",
            "surface_efficiency_score",
            "surface_required_move_ratio",
            "surface_term_structure_state",
            "surface_skew_state",
            "surface_implied_vs_realized_state",
            "universe_mode",
            "universe_snapshot_available_at",
            "experiment_id",
        ):
            labeled[field] = candidate.get(field)
        labeled["evaluation_end"] = evaluation_end.isoformat()
        labeled["evaluation_as_of"] = evaluation.isoformat()
        labeled["outcome_horizon_days"] = experiment_config.outcome_horizon_days
        outcomes.append(labeled)
    return outcomes, immature, skipped


def _outcome_stats(rows: list[Mapping[str, Any]], *, min_bucket_samples: int) -> dict[str, Any]:
    n = len(rows)
    numeric_end = [value for row in rows if (value := _number(row.get("end_return_pct"))) is not None]
    max_multiples = [value for row in rows if (value := _number(row.get("max_multiple"))) is not None]
    return {
        "samples": n,
        "target_hit_rate": 0.0 if n == 0 else round(sum(_bool(row.get("target_hit")) for row in rows) / n, 4),
        "touch_2x_rate": 0.0 if n == 0 else round(sum(_bool(row.get("touch_2x")) for row in rows) / n, 4),
        "touch_3x_rate": 0.0 if n == 0 else round(sum(_bool(row.get("touch_3x")) for row in rows) / n, 4),
        "full_loss_proxy_rate": 0.0 if n == 0 else round(sum(_bool(row.get("full_loss_proxy")) for row in rows) / n, 4),
        "mean_end_return_pct": None if not numeric_end else round(sum(numeric_end) / len(numeric_end), 4),
        "median_max_multiple": None if not max_multiples else round(median(max_multiples), 4),
        "valid_sample": n >= min_bucket_samples,
    }


def _research_times(values: Iterable[datetime], *, evaluation: datetime) -> list[datetime]:
    output: list[datetime] = []
    seen: set[str] = set()
    for value in values:
        timestamp = _aware(value, "research_time")
        if timestamp > evaluation:
            raise ValueError("research_time cannot be after evaluation_as_of")
        token = timestamp.isoformat()
        if token not in seen:
            seen.add(token)
            output.append(timestamp)
    output.sort()
    return output


def _experiment_id(
    *,
    times: list[datetime],
    evaluation: datetime,
    replay_config: ReplayConfig,
    experiment_config: HistoricalExperimentConfig,
    lineage: ExperimentLineage,
    universe_mode: str,
) -> str:
    payload = {
        "research_times": [value.isoformat() for value in times],
        "evaluation_as_of": evaluation.isoformat(),
        "replay_config": asdict(replay_config),
        "experiment_config": asdict(experiment_config),
        "lineage": asdict(lineage),
        "universe_mode": universe_mode,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return f"exp_{digest[:20]}"


def _latest_contract_quotes(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    clean = frame.copy()
    clean["event_ts"] = pd.to_datetime(clean["event_ts"], utc=True, errors="coerce")
    clean = clean.dropna(subset=["event_ts", "contract_symbol"]).sort_values("event_ts")
    return clean.groupby("contract_symbol", sort=False, as_index=False).tail(1).reset_index(drop=True)


def _rank_bucket(value: object) -> str | None:
    rank = _number(value)
    if rank is None or rank < 1:
        return None
    if rank == 1:
        return "rank_1"
    if rank <= 3:
        return "rank_2_3"
    return "rank_4_plus"


def _score_bucket(value: object) -> str | None:
    score = _number(value)
    if score is None:
        return None
    start = int(max(0, min(100, math.floor(score / 10.0) * 10)))
    return f"{start:02d}_{min(100, start + 10):02d}"


def _ratio_bucket(value: object) -> str | None:
    ratio = _number(value)
    if ratio is None or ratio < 0:
        return None
    if ratio < 0.75:
        return "<0.75x"
    if ratio < 1.0:
        return "0.75_1.0x"
    if ratio < 1.25:
        return "1.0_1.25x"
    if ratio <= 1.75:
        return "1.25_1.75x"
    return ">1.75x"


def _surface_efficiency_bucket(value: object) -> str | None:
    score = _number(value)
    if score is None:
        return None
    if score < 35:
        return "weak_<35"
    if score < 60:
        return "middle_35_60"
    return "strong_>=60"


def _surface_move_bucket(value: object) -> str | None:
    ratio = _number(value)
    if ratio is None or ratio < 0:
        return None
    if ratio < 1:
        return "<1.0x"
    if ratio < 1.5:
        return "1.0_1.5x"
    if ratio <= 2:
        return "1.5_2.0x"
    return ">2.0x"


def _symbols(values: Iterable[object]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        symbol = str(value or "").strip().upper()
        if symbol.endswith(".US"):
            symbol = symbol[:-3]
        if symbol and symbol not in seen:
            seen.add(symbol)
            output.append(symbol)
    return output


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        return None
    return pd.Timestamp(parsed).to_pydatetime()


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _positive(value: object) -> float | None:
    number = _number(value)
    return number if number is not None and number > 0 else None


def _text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _optional_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    token = str(value).strip().lower()
    if token in {"true", "1", "yes"}:
        return True
    if token in {"false", "0", "no"}:
        return False
    return None


def _bool(value: object) -> bool:
    return bool(_optional_bool(value))


__all__ = [
    "ExperimentLineage",
    "HistoricalExperimentConfig",
    "run_historical_research_experiment",
    "summarize_historical_experiment",
]
