"""Compact dashboard and alert decisions for personal options research.

The output is deliberately transport-neutral: terminal, web UI, Slack/email or a
future notification worker can all consume the same JSON envelope. This module
never sends a message or submits a broker order by itself.
"""

from __future__ import annotations

from typing import Any, Mapping


_DECISION_PRIORITY = {
    "NO_TRADE": 0,
    "PASS": 0,
    "WATCH": 1,
    "TRADE_READY_RESEARCH": 2,
}


def build_personal_dashboard(
    personal_report: Mapping[str, Any],
    *,
    funnel: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Build a small control-room payload from the personal decision report."""
    candidates = [dict(row) for row in personal_report.get("candidates", []) if isinstance(row, Mapping)]
    cards: list[dict[str, Any]] = []
    for row in candidates:
        ev = row.get("empirical_ev") if isinstance(row.get("empirical_ev"), Mapping) else {}
        quality = row.get("option_quality") if isinstance(row.get("option_quality"), Mapping) else {}
        quality_metrics = quality.get("metrics") if isinstance(quality.get("metrics"), Mapping) else {}
        surface = quality.get("surface_context") if isinstance(quality.get("surface_context"), Mapping) else {}
        risk = row.get("risk") if isinstance(row.get("risk"), Mapping) else {}
        data_quality = row.get("data_quality") if isinstance(row.get("data_quality"), Mapping) else {}
        cards.append(
            {
                "decision": row.get("decision"),
                "symbol": row.get("symbol"),
                "contract_symbol": row.get("contract_symbol"),
                "direction": row.get("direction"),
                "composite_score": row.get("composite_score"),
                "ranking_score": row.get("ranking_score"),
                "option_quality_score": row.get("option_quality_score"),
                "regime_fit_score": row.get("regime_fit_score"),
                "historical_samples": ev.get("samples"),
                "historical_target_hit_rate": ev.get("empirical_target_hit_rate"),
                "expected_return_pct": ev.get("expected_return_pct"),
                "lower_confidence_bound_pct": ev.get("lower_confidence_bound_pct"),
                "entry_ask": row.get("entry_ask"),
                "max_loss_usd_per_contract": row.get("max_loss_usd_per_contract"),
                "configured_contract_cap": row.get("max_contracts_within_configured_single_trade_risk"),
                "risk_approved": risk.get("approved"),
                "spread_pct": quality_metrics.get("spread_pct"),
                "iv_percentile": quality_metrics.get("iv_percentile"),
                "surface_efficiency_score": quality_metrics.get("surface_efficiency_score"),
                "surface_required_move_ratio": quality_metrics.get("surface_required_move_ratio"),
                "candidate_iv_premium_to_atm_points": quality_metrics.get("candidate_iv_premium_to_atm_points"),
                "surface_atm_expected_move_pct": surface.get("atm_expected_move_pct"),
                "surface_term_structure_state": quality_metrics.get("surface_term_structure_state"),
                "surface_skew_state": quality_metrics.get("surface_skew_state"),
                "surface_implied_vs_realized_state": quality_metrics.get("surface_implied_vs_realized_state"),
                "data_feed": data_quality.get("option_feed"),
                "hard_reasons": row.get("hard_reasons") or [],
                "watch_reasons": row.get("watch_reasons") or [],
            }
        )

    decision = str(personal_report.get("decision") or "NO_TRADE")
    regime = personal_report.get("regime") if isinstance(personal_report.get("regime"), Mapping) else {}
    return {
        "headline": _headline(decision, cards),
        "decision": decision,
        "market_regime": regime.get("regime", "unknown"),
        "market_regime_confidence": regime.get("confidence"),
        "funnel": dict(funnel or {}),
        "cards": cards,
        "trade_ready_count": sum(card.get("decision") == "TRADE_READY_RESEARCH" for card in cards),
        "watch_count": sum(card.get("decision") == "WATCH" for card in cards),
        "disclaimer": "Research dashboard only; broker execution is separate.",
    }


def build_alert_event(
    current_dashboard: Mapping[str, Any],
    previous_dashboard: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Emit an alert only for a meaningful promotion/change in research state."""
    previous_dashboard = previous_dashboard or {}
    current_decision = str(current_dashboard.get("decision") or "NO_TRADE")
    previous_decision = str(previous_dashboard.get("decision") or "NO_TRADE")
    current_priority = _DECISION_PRIORITY.get(current_decision, 0)
    previous_priority = _DECISION_PRIORITY.get(previous_decision, 0)

    current_top = _top_contract(current_dashboard)
    previous_top = _top_contract(previous_dashboard)
    reasons: list[str] = []
    if current_priority > previous_priority:
        reasons.append("decision_promoted")
    if current_decision == "TRADE_READY_RESEARCH" and current_top and current_top != previous_top:
        reasons.append("new_trade_ready_contract")
    if current_decision == "WATCH" and current_top and current_top != previous_top:
        reasons.append("new_top_watch")

    should_alert = bool(reasons)
    return {
        "should_alert": should_alert,
        "reasons": reasons,
        "decision": current_decision,
        "top_contract": current_top,
        "message": _alert_message(current_dashboard, reasons) if should_alert else None,
    }


def _headline(decision: str, cards: list[dict[str, Any]]) -> str:
    if decision == "TRADE_READY_RESEARCH":
        count = sum(card.get("decision") == "TRADE_READY_RESEARCH" for card in cards)
        return f"{count} research candidate{'s' if count != 1 else ''} passed all configured gates"
    if decision == "WATCH":
        return "No trade-ready setup; strongest candidates remain on watch"
    return "NO TRADE — no candidate passed the configured evidence and risk gates"


def _top_contract(dashboard: Mapping[str, Any]) -> str | None:
    cards = dashboard.get("cards")
    if not isinstance(cards, list) or not cards:
        return None
    first = cards[0]
    if not isinstance(first, Mapping):
        return None
    value = str(first.get("contract_symbol") or "").strip().upper()
    return value or None


def _alert_message(dashboard: Mapping[str, Any], reasons: list[str]) -> str:
    top = _top_contract(dashboard)
    decision = str(dashboard.get("decision") or "NO_TRADE")
    regime = str(dashboard.get("market_regime") or "unknown")
    suffix = f" Top: {top}." if top else ""
    return f"Vibe-Trading research changed to {decision} in {regime} regime.{suffix} Reasons: {', '.join(reasons)}"


__all__ = ["build_alert_event", "build_personal_dashboard"]
