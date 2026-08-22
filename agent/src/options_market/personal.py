"""Personal-account research decision layer for the U.S. options stack.

The layer turns an institutional-style shortlist into a small, explainable set of
personal research decisions.  It never places an order.  ``TRADE_READY_RESEARCH``
means every configured research/risk gate passed; actual broker execution remains
behind the separate paper/live execution boundary.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any, Iterable, Mapping, Sequence

from .regime import regime_direction_fit
from .risk import PortfolioRiskConfig, assess_portfolio_risk
from .volatility import OptionQualityConfig, assess_option_quality


@dataclass(frozen=True)
class PersonalDecisionConfig:
    min_ranking_score: float = 65.0
    min_option_quality_score: float = 60.0
    min_regime_fit_score: float = 35.0
    max_display_candidates: int = 3
    max_trade_ready_candidates: int = 2
    max_contracts_per_trade: int = 1
    require_positive_ev: bool = True
    require_walk_forward_pass: bool = True
    allow_watch_when_calibration_missing: bool = True

    def validate(self) -> None:
        for name, value in (
            ("min_ranking_score", self.min_ranking_score),
            ("min_option_quality_score", self.min_option_quality_score),
            ("min_regime_fit_score", self.min_regime_fit_score),
        ):
            if not 0 <= value <= 100:
                raise ValueError(f"{name} must be between 0 and 100")
        if not 1 <= self.max_display_candidates <= 20:
            raise ValueError("max_display_candidates must be between 1 and 20")
        if not 1 <= self.max_trade_ready_candidates <= self.max_display_candidates:
            raise ValueError("max_trade_ready_candidates must be <= max_display_candidates")
        if not 1 <= self.max_contracts_per_trade <= 20:
            raise ValueError("max_contracts_per_trade must be between 1 and 20")


def evaluate_personal_candidate(
    candidate: Mapping[str, Any],
    *,
    account_equity_usd: float,
    existing_positions: Iterable[Mapping[str, Any]] = (),
    ev_report: Mapping[str, Any] | None = None,
    walk_forward_report: Mapping[str, Any] | None = None,
    regime_report: Mapping[str, Any] | None = None,
    option_quality_report: Mapping[str, Any] | None = None,
    catalyst_score: float | None = None,
    realized_vol_pct: float | None = None,
    iv_percentile: float | None = None,
    config: PersonalDecisionConfig | None = None,
    risk_config: PortfolioRiskConfig | None = None,
    option_quality_config: OptionQualityConfig | None = None,
) -> dict[str, Any]:
    """Evaluate one candidate using evidence, context, contract quality and risk."""
    cfg = config or PersonalDecisionConfig()
    cfg.validate()
    rcfg = risk_config or PortfolioRiskConfig(
        require_positive_ev=cfg.require_positive_ev,
        require_walk_forward_pass=cfg.require_walk_forward_pass,
    )

    ranking = _score(candidate.get("ranking_score") or candidate.get("score"))
    direction = str(candidate.get("direction") or "").strip().lower()
    catalyst = _score_or_none(catalyst_score if catalyst_score is not None else candidate.get("catalyst_score"))

    quality = dict(option_quality_report or assess_option_quality(
        candidate,
        catalyst_score=catalyst,
        realized_vol_pct=realized_vol_pct,
        iv_percentile=iv_percentile,
        config=option_quality_config,
    ))
    quality_score = _score(quality.get("quality_score"))

    regime = dict(regime_report or {"regime": "unknown", "confidence": 0.0})
    regime_fit = regime_direction_fit(regime, direction)
    regime_score = _score(regime_fit.get("score"))

    ev = dict(ev_report or {})
    walk = dict(walk_forward_report or {})
    ev_present = bool(ev)
    walk_present = bool(walk)
    positive_ev = bool(ev.get("positive_ev"))
    walk_pass = walk.get("decision") == "WALK_FORWARD_PASS"

    risk = assess_portfolio_risk(
        candidate,
        account_equity_usd=account_equity_usd,
        quantity=1,
        existing_positions=existing_positions,
        ev_report=ev,
        walk_forward_report=walk,
        config=rcfg,
    )

    hard_reasons: list[str] = []
    watch_reasons: list[str] = []
    if ranking < cfg.min_ranking_score:
        hard_reasons.append("ranking_score_below_personal_gate")
    if not bool(quality.get("passed")) or quality_score < cfg.min_option_quality_score:
        hard_reasons.append("option_quality_failed")
    if regime_score < cfg.min_regime_fit_score:
        hard_reasons.append("market_regime_strongly_conflicts")

    if cfg.require_positive_ev:
        if not ev_present:
            watch_reasons.append("empirical_ev_not_available")
        elif not bool(ev.get("calibration_valid")):
            watch_reasons.append("empirical_ev_sample_insufficient")
        elif not positive_ev:
            hard_reasons.append("empirical_ev_not_positive")
    if cfg.require_walk_forward_pass:
        if not walk_present:
            watch_reasons.append("walk_forward_not_available")
        elif not walk_pass:
            hard_reasons.append("walk_forward_failed")

    if not risk.get("approved"):
        risk_reasons = list(risk.get("reasons") or [])
        evidence_only = {"positive_ev_required", "walk_forward_pass_required"}
        non_evidence = [reason for reason in risk_reasons if reason not in evidence_only]
        if non_evidence:
            hard_reasons.extend(f"risk:{reason}" for reason in non_evidence)
        elif risk_reasons and not watch_reasons:
            hard_reasons.extend(f"risk:{reason}" for reason in risk_reasons)

    evidence_score = _evidence_score(ev, walk)
    catalyst_component = 50.0 if catalyst is None else catalyst
    composite = (
        0.35 * ranking
        + 0.25 * quality_score
        + 0.20 * evidence_score
        + 0.10 * regime_score
        + 0.10 * catalyst_component
    )

    if hard_reasons:
        decision = "PASS"
    elif watch_reasons:
        decision = "WATCH" if cfg.allow_watch_when_calibration_missing else "PASS"
    else:
        decision = "TRADE_READY_RESEARCH"

    max_contracts = _max_contracts_for_risk(
        candidate,
        account_equity_usd=account_equity_usd,
        max_risk_pct=rcfg.max_risk_per_trade_pct,
        hard_cap=cfg.max_contracts_per_trade,
    )

    return {
        "decision": decision,
        "symbol": candidate.get("symbol") or candidate.get("ticker"),
        "contract_symbol": candidate.get("contract_symbol"),
        "direction": direction,
        "option_type": candidate.get("option_type"),
        "composite_score": round(composite, 2),
        "ranking_score": round(ranking, 2),
        "option_quality_score": round(quality_score, 2),
        "regime_fit_score": round(regime_score, 2),
        "evidence_score": round(evidence_score, 2),
        "catalyst_score": catalyst,
        "target_profit_pct": candidate.get("target_profit_pct"),
        "target_multiple": candidate.get("target_multiple"),
        "entry_ask": candidate.get("entry_ask"),
        "max_loss_usd_per_contract": candidate.get("max_loss_usd"),
        "max_contracts_within_configured_single_trade_risk": max_contracts,
        "hard_reasons": _dedupe(hard_reasons),
        "watch_reasons": _dedupe(watch_reasons),
        "option_quality": quality,
        "market_regime": regime,
        "regime_fit": regime_fit,
        "empirical_ev": ev or None,
        "walk_forward": walk or None,
        "risk": risk,
        "config": asdict(cfg),
        "interpretation": (
            "Research decision only. TRADE_READY_RESEARCH means configured evidence and risk gates passed; "
            "it is not an instruction, guarantee, or automatic broker order."
        ),
    }


def build_personal_shortlist(
    candidates: Sequence[Mapping[str, Any]],
    *,
    account_equity_usd: float,
    existing_positions: Iterable[Mapping[str, Any]] = (),
    ev_reports: Mapping[str, Mapping[str, Any]] | None = None,
    walk_forward_reports: Mapping[str, Mapping[str, Any]] | None = None,
    regime_report: Mapping[str, Any] | None = None,
    option_quality_reports: Mapping[str, Mapping[str, Any]] | None = None,
    realized_vol_by_symbol: Mapping[str, float] | None = None,
    iv_percentile_by_contract: Mapping[str, float] | None = None,
    config: PersonalDecisionConfig | None = None,
    risk_config: PortfolioRiskConfig | None = None,
) -> dict[str, Any]:
    """Reduce a research shortlist to at most a few personal decision cards."""
    cfg = config or PersonalDecisionConfig()
    cfg.validate()
    ev_reports = ev_reports or {}
    walk_forward_reports = walk_forward_reports or {}
    option_quality_reports = option_quality_reports or {}
    realized_vol_by_symbol = realized_vol_by_symbol or {}
    iv_percentile_by_contract = iv_percentile_by_contract or {}
    positions = [dict(row) for row in existing_positions]

    evaluated: list[dict[str, Any]] = []
    for candidate in candidates:
        key = _candidate_key(candidate)
        symbol = str(candidate.get("symbol") or candidate.get("ticker") or "").upper()
        evaluated.append(
            evaluate_personal_candidate(
                candidate,
                account_equity_usd=account_equity_usd,
                existing_positions=positions,
                ev_report=ev_reports.get(key) or ev_reports.get(symbol),
                walk_forward_report=walk_forward_reports.get(key) or walk_forward_reports.get(symbol),
                regime_report=regime_report,
                option_quality_report=option_quality_reports.get(key),
                catalyst_score=_score_or_none(candidate.get("catalyst_score")),
                realized_vol_pct=realized_vol_by_symbol.get(symbol),
                iv_percentile=iv_percentile_by_contract.get(key),
                config=cfg,
                risk_config=risk_config,
            )
        )

    priority = {"TRADE_READY_RESEARCH": 0, "WATCH": 1, "PASS": 2}
    evaluated.sort(key=lambda row: (priority.get(str(row["decision"]), 9), -float(row["composite_score"])))

    trade_ready_seen = 0
    visible: list[dict[str, Any]] = []
    for row in evaluated:
        if row["decision"] == "TRADE_READY_RESEARCH":
            trade_ready_seen += 1
            if trade_ready_seen > cfg.max_trade_ready_candidates:
                row = {**row, "decision": "WATCH", "watch_reasons": [*row["watch_reasons"], "daily_trade_ready_cap"]}
        if len(visible) < cfg.max_display_candidates:
            visible.append(row)

    ready = [row for row in visible if row["decision"] == "TRADE_READY_RESEARCH"]
    watches = [row for row in visible if row["decision"] == "WATCH"]
    overall = "TRADE_READY_RESEARCH" if ready else ("WATCH" if watches else "NO_TRADE")
    return {
        "mode": "personal_options_research",
        "decision": overall,
        "trade_ready_count": len(ready),
        "watch_count": len(watches),
        "candidate_count_evaluated": len(evaluated),
        "candidates": visible,
        "regime": dict(regime_report or {"regime": "unknown"}),
        "config": asdict(cfg),
        "warning": (
            "Personal research layer only. It is deliberately selective and may return NO_TRADE. "
            "Historical evidence can fail in future regimes; broker execution is separate."
        ),
    }


def _evidence_score(ev: Mapping[str, Any], walk: Mapping[str, Any]) -> float:
    if not ev:
        return 25.0
    if not bool(ev.get("calibration_valid")):
        return 30.0
    lower = _finite(ev.get("lower_confidence_bound_pct")) or 0.0
    hit = _finite(ev.get("empirical_target_hit_rate")) or 0.0
    ev_base = 50.0 + max(-35.0, min(35.0, lower * 1.5)) + min(15.0, max(0.0, hit) * 50.0)
    if bool(ev.get("positive_ev")):
        ev_base += 10.0
    if walk.get("decision") == "WALK_FORWARD_PASS":
        ev_base += 10.0
    elif walk:
        ev_base -= 20.0
    return min(100.0, max(0.0, ev_base))


def _max_contracts_for_risk(
    candidate: Mapping[str, Any],
    *,
    account_equity_usd: float,
    max_risk_pct: float,
    hard_cap: int,
) -> int:
    equity = _positive(account_equity_usd)
    loss = _positive(candidate.get("max_loss_usd"))
    if loss is None:
        ask = _positive(candidate.get("entry_ask") or candidate.get("ask"))
        loss = None if ask is None else ask * 100.0
    if equity is None or loss is None:
        return 0
    budget = equity * max_risk_pct / 100.0
    return max(0, min(hard_cap, int(budget // loss)))


def _candidate_key(candidate: Mapping[str, Any]) -> str:
    contract = str(candidate.get("contract_symbol") or "").strip().upper()
    if contract:
        return contract
    return str(candidate.get("symbol") or candidate.get("ticker") or "").strip().upper()


def _score(value: object) -> float:
    number = _finite(value)
    return 0.0 if number is None else min(100.0, max(0.0, number))


def _score_or_none(value: object) -> float | None:
    number = _finite(value)
    return None if number is None else min(100.0, max(0.0, number))


def _positive(value: object) -> float | None:
    number = _finite(value)
    return number if number is not None and number > 0 else None


def _finite(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _dedupe(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if str(value)))


__all__ = ["PersonalDecisionConfig", "build_personal_shortlist", "evaluate_personal_candidate"]
