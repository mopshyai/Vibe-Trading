"""Staged ranking for U.S. options research candidates.

This module combines the cheap cross-sectional chart screen with the existing
per-contract options opportunity score. Catalyst data is optional and injected;
there is no news/provider coupling in the ranking core.

No score in this module is a probability or expected return.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class OptionsMarketPipelineConfig:
    """Gates and weights for chart -> options -> catalyst ranking."""

    target_profit_pct: float = 300.0
    min_chart_score: float = 60.0
    min_option_score: float = 55.0
    max_required_move_vs_one_sigma: float = 1.75
    chart_weight: float = 0.44
    option_weight: float = 0.56
    chart_weight_with_catalyst: float = 0.35
    option_weight_with_catalyst: float = 0.45
    catalyst_weight: float = 0.20
    final_top_n: int = 10

    def validate(self) -> None:
        if self.target_profit_pct <= 0:
            raise ValueError("target_profit_pct must be positive")
        for name, value in (
            ("min_chart_score", self.min_chart_score),
            ("min_option_score", self.min_option_score),
        ):
            if not 0 <= value <= 100:
                raise ValueError(f"{name} must be between 0 and 100")
        if self.max_required_move_vs_one_sigma <= 0:
            raise ValueError("max_required_move_vs_one_sigma must be positive")
        if abs(self.chart_weight + self.option_weight - 1.0) > 1e-9:
            raise ValueError("chart_weight + option_weight must equal 1")
        if abs(
            self.chart_weight_with_catalyst
            + self.option_weight_with_catalyst
            + self.catalyst_weight
            - 1.0
        ) > 1e-9:
            raise ValueError("weights with catalyst must equal 1")
        if not 1 <= self.final_top_n <= 100:
            raise ValueError("final_top_n must be between 1 and 100")


def combine_rankings(
    chart_candidates: Sequence[Mapping[str, Any]],
    options_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    catalyst_scores: Mapping[str, float] | None = None,
    config: OptionsMarketPipelineConfig | None = None,
) -> dict[str, Any]:
    """Combine chart and contract scores into a final research shortlist."""
    cfg = config or OptionsMarketPipelineConfig()
    cfg.validate()

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    catalyst_scores = catalyst_scores or {}

    for chart in chart_candidates:
        symbol = str(chart.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        chart_score = _score(chart.get("chart_score"))
        if chart_score < cfg.min_chart_score:
            rejected.append({"symbol": symbol, "reason": "chart_score_below_gate"})
            continue

        direction = str(chart.get("direction") or "").strip().lower()
        if direction not in {"bullish", "bearish"}:
            rejected.append({"symbol": symbol, "reason": "invalid_chart_direction"})
            continue
        expected_option_type = "call" if direction == "bullish" else "put"
        contracts = options_by_symbol.get(symbol, ())
        matched = 0
        eligible = 0
        for option in contracts:
            if str(option.get("option_type") or "").lower() != expected_option_type:
                continue
            target_profit = _positive_float(option.get("target_profit_pct"), default=cfg.target_profit_pct)
            if target_profit != cfg.target_profit_pct:
                continue
            matched += 1
            option_score = _score(option.get("score"))
            move_ratio = _positive_float(option.get("required_move_vs_one_sigma"), default=999.0)
            if option_score < cfg.min_option_score:
                continue
            if move_ratio > cfg.max_required_move_vs_one_sigma:
                continue
            eligible += 1

            catalyst = _bounded_score_or_none(catalyst_scores.get(symbol))
            if catalyst is None:
                ranking_score = cfg.chart_weight * chart_score + cfg.option_weight * option_score
            else:
                ranking_score = (
                    cfg.chart_weight_with_catalyst * chart_score
                    + cfg.option_weight_with_catalyst * option_score
                    + cfg.catalyst_weight * catalyst
                )

            # Preserve the point-in-time contract evidence used by downstream
            # quality, risk, journal and attribution layers. Ranking may add
            # fields, but must not silently strip Greeks/provenance/surface data.
            accepted.append(
                {
                    "symbol": symbol,
                    "direction": chart.get("direction"),
                    "setup_type": chart.get("setup_type"),
                    "chart_rank": chart.get("rank"),
                    "chart_score": round(chart_score, 2),
                    "realized_vol20_pct": chart.get("realized_vol20_pct"),
                    "contract_symbol": option.get("contract_symbol"),
                    "option_type": expected_option_type,
                    "option_score": round(option_score, 2),
                    "ranking_score": round(ranking_score, 2),
                    "target_profit_pct": cfg.target_profit_pct,
                    "target_multiple": round(1.0 + cfg.target_profit_pct / 100.0, 4),
                    "spot": option.get("spot"),
                    "strike": option.get("strike"),
                    "expiration": option.get("expiration"),
                    "dte": option.get("dte"),
                    "bid": option.get("bid"),
                    "entry_ask": option.get("entry_ask"),
                    "max_loss_usd": option.get("max_loss_usd"),
                    "target_premium": option.get("target_premium"),
                    "target_underlying_at_expiry": option.get("target_underlying_at_expiry"),
                    "required_underlying_move_pct": option.get("required_underlying_move_pct"),
                    "one_sigma_implied_move_pct": option.get("one_sigma_implied_move_pct"),
                    "required_move_vs_one_sigma": move_ratio,
                    "spread_pct": option.get("spread_pct"),
                    "open_interest": option.get("open_interest"),
                    "volume": option.get("volume"),
                    "implied_volatility": option.get("implied_volatility"),
                    "delta": option.get("delta"),
                    "gamma": option.get("gamma"),
                    "theta": option.get("theta"),
                    "vega": option.get("vega"),
                    "surface_context": option.get("surface_context"),
                    "surface_efficiency_score": option.get("surface_efficiency_score"),
                    "surface_iv_percentile": option.get("surface_iv_percentile"),
                    "surface_required_move_ratio": option.get("surface_required_move_ratio"),
                    "data_source": option.get("data_source"),
                    "option_feed": option.get("option_feed"),
                    "execution_grade_feed": option.get("execution_grade_feed"),
                    "data_warnings": list(option.get("data_warnings") or []),
                    "catalyst_score": None if catalyst is None else round(catalyst, 2),
                    "score_interpretation": "ranking score only; not probability, expected return, or guarantee",
                }
            )

        if matched == 0:
            rejected.append({"symbol": symbol, "reason": f"no_{expected_option_type}_contracts_for_direction"})
        elif eligible == 0:
            rejected.append({"symbol": symbol, "reason": "directional_contracts_failed_option_gates"})

    accepted.sort(
        key=lambda row: (
            -float(row["ranking_score"]),
            float(row["required_move_vs_one_sigma"]),
            float(row.get("spread_pct") or 999.0),
        )
    )
    selected = accepted[: cfg.final_top_n]
    for rank, row in enumerate(selected, start=1):
        row["final_rank"] = rank

    return {
        "mode": "us_options_market_shortlist",
        "target_profit_pct": cfg.target_profit_pct,
        "target_multiple": 1.0 + cfg.target_profit_pct / 100.0,
        "decision": "NO_TRADE" if not selected else "RESEARCH_CANDIDATES",
        "candidate_count": len(selected),
        "candidates": selected,
        "rejected": rejected,
        "config": asdict(cfg),
        "warning": (
            "Research shortlist only. A 300% target is a payoff scenario, not a forecast. "
            "Scores rank supplied candidates and must be calibrated with out-of-sample historical outcomes, "
            "transaction costs, slippage, and option-surface history before any live use."
        ),
    }


def _score(value: object) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return min(100.0, max(0.0, result))


def _positive_float(value: object, *, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if result >= 0 else default


def _bounded_score_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return min(100.0, max(0.0, result))


__all__ = ["OptionsMarketPipelineConfig", "combine_rankings"]
