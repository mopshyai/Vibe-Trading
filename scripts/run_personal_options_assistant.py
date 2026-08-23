#!/usr/bin/env python3
"""Turn the continuous U.S. options shortlist into a personal research dashboard.

This command is broker-mutation free. It can read the configured Alpaca PAPER
account/positions through the hosted REST reader and reports eligibility for the
separate supervised paper lifecycle. Missing EV/walk-forward evidence produces
WATCH/PASS rather than silently promoting a candidate to trade-ready.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any, Mapping

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent"
if str(AGENT) not in sys.path:
    sys.path.insert(0, str(AGENT))

from src.options_market.dashboard import build_alert_event, build_personal_dashboard  # noqa: E402
from src.options_market.personal import build_personal_shortlist  # noqa: E402
from src.options_market.personal_service import build_execution_readiness  # noqa: E402
from src.options_market.regime import classify_market_regime  # noqa: E402
from src.trading_platform.paper_broker_read import fetch_paper_account_positions  # noqa: E402
from src.trading_platform.runtime_config import load_alpaca_runtime_config  # noqa: E402

_OCC = re.compile(r"^(?P<root>[A-Z0-9]{1,6})(?P<date>\d{6})(?P<type>[CP])(?P<strike>\d{8})$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Personal options research dashboard")
    parser.add_argument("--analysis-json", required=True, type=Path, help="Latest continuous-analysis JSON output")
    account = parser.add_mutually_exclusive_group(required=True)
    account.add_argument("--account-equity", type=float, help="Account equity used by the research risk gate")
    account.add_argument("--alpaca-account", action="store_true", help="Read equity and open long-option positions from Alpaca PAPER")
    parser.add_argument("--positions-json", type=Path, help="Existing risk positions for the portfolio-risk gate")
    parser.add_argument(
        "--assume-no-open-risk",
        action="store_true",
        help="Explicitly state there are no existing long-option premium-risk positions",
    )
    parser.add_argument("--ev-json", type=Path, help="contract/symbol -> empirical EV report mapping")
    parser.add_argument("--walkforward-json", type=Path, help="contract/symbol -> walk-forward report mapping")
    parser.add_argument("--regime-json", type=Path, help="Precomputed market-regime report")
    parser.add_argument("--benchmark-csv", type=Path, help="Long-form benchmark bars with symbol,date,close")
    parser.add_argument("--bullish-breadth-pct", type=float)
    parser.add_argument("--realized-vol-json", type=Path, help="symbol -> realized volatility percent")
    parser.add_argument("--iv-percentile-json", type=Path, help="contract -> IV percentile")
    parser.add_argument("--previous-dashboard", type=Path, help="Prior dashboard JSON for meaningful-change alerting")
    parser.add_argument("--output", type=Path, help="Write full result JSON here")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    analysis = _object(args.analysis_json)
    candidates = _extract_candidates(analysis)

    if args.alpaca_account:
        account_equity, positions = _alpaca_risk_state()
    else:
        account_equity = float(args.account_equity)
        if args.positions_json:
            positions = _list(args.positions_json)
        elif args.assume_no_open_risk:
            positions = []
        else:
            raise SystemExit("Pass --positions-json or explicitly use --assume-no-open-risk.")

    regime = _load_regime(args)
    personal = build_personal_shortlist(
        candidates,
        account_equity_usd=account_equity,
        existing_positions=positions,
        ev_reports=_mapping(args.ev_json),
        walk_forward_reports=_mapping(args.walkforward_json),
        regime_report=regime,
        realized_vol_by_symbol=_numeric_mapping(args.realized_vol_json),
        iv_percentile_by_contract=_numeric_mapping(args.iv_percentile_json),
    )
    personal = _enrich_journal_surface(personal, candidates)
    dashboard = build_personal_dashboard(personal, funnel=_funnel(analysis))
    previous = _object(args.previous_dashboard) if args.previous_dashboard else None
    alert = build_alert_event(dashboard, previous)
    result = {
        "mode": "personal_options_assistant",
        "account_equity_usd": account_equity,
        "existing_option_risk_positions": len(positions),
        "personal": personal,
        "dashboard": dashboard,
        "alert": alert,
        "execution": build_execution_readiness(personal),
    }
    rendered = json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False, default=str) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


def _extract_candidates(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    paths = [
        payload.get("final_stage"),
        payload.get("personal"),
        payload,
    ]
    for node in paths:
        if isinstance(node, Mapping) and isinstance(node.get("candidates"), list):
            return [dict(row) for row in node["candidates"] if isinstance(row, Mapping)]
    state = payload.get("state")
    if isinstance(state, Mapping) and isinstance(state.get("latest_shortlist"), list):
        return [dict(row) for row in state["latest_shortlist"] if isinstance(row, Mapping)]
    return []


def _enrich_journal_surface(
    personal: Mapping[str, Any],
    source_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Keep decision-time surface evidence on every journal candidate.

    The compact personal journal projection predates surface analysis. Enriching
    it here preserves point-in-time metrics without changing ranking decisions or
    leaking later outcome information into the decision row.
    """
    result = dict(personal)
    rows = personal.get("journal_candidates") if isinstance(personal.get("journal_candidates"), list) else []
    by_contract = {
        str(row.get("contract_symbol") or "").strip().upper(): row
        for row in source_candidates
        if str(row.get("contract_symbol") or "").strip()
    }
    enriched: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        source = by_contract.get(str(row.get("contract_symbol") or "").strip().upper())
        if isinstance(source, Mapping):
            surface = source.get("surface_context") if isinstance(source.get("surface_context"), Mapping) else {}
            row.update(
                {
                    "surface_efficiency_score": source.get("surface_efficiency_score") or surface.get("surface_efficiency_score"),
                    "surface_required_move_ratio": source.get("surface_required_move_ratio") or surface.get("required_move_vs_surface_expected_move"),
                    "surface_iv_percentile": source.get("surface_iv_percentile") or surface.get("surface_iv_percentile"),
                    "candidate_iv_premium_to_atm_points": surface.get("candidate_iv_premium_to_atm_points"),
                    "surface_atm_expected_move_pct": surface.get("atm_expected_move_pct"),
                    "surface_term_structure_state": surface.get("term_structure_state"),
                    "surface_skew_state": surface.get("skew_state"),
                    "surface_implied_vs_realized_state": surface.get("implied_vs_realized_state"),
                }
            )
        enriched.append(row)
    result["journal_candidates"] = enriched
    return result


def _load_regime(args: argparse.Namespace) -> dict[str, Any]:
    if args.regime_json:
        return _object(args.regime_json)
    if not args.benchmark_csv:
        return {"regime": "unknown", "confidence": 0.0, "warnings": ["regime_input_not_supplied"]}
    frame = pd.read_csv(args.benchmark_csv)
    required = {"symbol", "date", "close"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"benchmark CSV missing columns: {sorted(missing)}")
    frame["date"] = pd.to_datetime(frame["date"], utc=True, errors="coerce")
    frame = frame.dropna(subset=["symbol", "date", "close"])
    benchmarks: dict[str, pd.DataFrame] = {}
    for symbol, group in frame.groupby("symbol", sort=False):
        benchmarks[str(symbol).upper()] = group.set_index("date").sort_index()
    return classify_market_regime(
        benchmarks,
        bullish_breadth_pct=args.bullish_breadth_pct,
    )


def _alpaca_risk_state() -> tuple[float, list[dict[str, Any]]]:
    broker = fetch_paper_account_positions(load_alpaca_runtime_config())
    account = broker.get("account") if isinstance(broker.get("account"), Mapping) else None
    if not isinstance(account, Mapping):
        raise RuntimeError("Alpaca paper account snapshot missing account data")
    if bool(account.get("trading_blocked")) or bool(account.get("account_blocked")) or bool(account.get("trade_suspended_by_user")):
        raise RuntimeError("Alpaca paper account is trading-blocked")
    equity = _positive(account.get("equity") or account.get("portfolio_value"))
    if equity is None:
        raise RuntimeError("Alpaca paper account snapshot missing positive equity")

    rows = broker.get("positions") if isinstance(broker, Mapping) else None
    risk_positions: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping) or str(row.get("asset_class") or "").lower() != "option":
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        match = _OCC.match(symbol)
        if match is None:
            continue
        side = str(row.get("side") or "").lower()
        qty = _positive(row.get("qty") or row.get("quantity"))
        basis = _positive(row.get("cost_basis"))
        avg = _positive(row.get("avg_entry_price") or row.get("average_cost"))
        if side not in {"long", "buy", ""} or qty is None:
            continue
        risk_usd = basis if basis is not None else (avg * 100.0 * qty if avg is not None else None)
        if risk_usd is None:
            continue
        risk_positions.append(
            {
                "symbol": match.group("root"),
                "underlying": match.group("root"),
                "contract_symbol": symbol,
                "asset_class": "option",
                "option_type": "call" if match.group("type") == "C" else "put",
                "expiration": _occ_expiration(match.group("date")),
                "quantity": qty,
                "risk_usd": risk_usd,
                "premium_risk_usd": risk_usd,
                "cost_basis": risk_usd,
            }
        )
    return equity, risk_positions


def _funnel(analysis: Mapping[str, Any]) -> dict[str, int]:
    chart = analysis.get("chart_stage") if isinstance(analysis.get("chart_stage"), Mapping) else {}
    final = analysis.get("final_stage") if isinstance(analysis.get("final_stage"), Mapping) else analysis
    return {
        "universe": int(chart.get("universe_received") or chart.get("universe_requested") or 0),
        "chart_eligible": int(chart.get("eligible_for_cross_section") or 0),
        "chart_candidates": int(chart.get("candidate_count") or 0),
        "final_candidates": int(final.get("candidate_count") or 0) if isinstance(final, Mapping) else 0,
    }


def _object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"{path} must contain a JSON object")
    return dict(payload)


def _list(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"{path} must contain a JSON list")
    return [dict(row) for row in payload if isinstance(row, Mapping)]


def _mapping(path: Path | None) -> dict[str, Mapping[str, Any]]:
    if path is None:
        return {}
    payload = _object(path)
    return {str(key).upper(): value for key, value in payload.items() if isinstance(value, Mapping)}


def _numeric_mapping(path: Path | None) -> dict[str, float]:
    if path is None:
        return {}
    payload = _object(path)
    output: dict[str, float] = {}
    for key, value in payload.items():
        try:
            output[str(key).upper()] = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
    return output


def _positive(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number > 0 else None


def _occ_expiration(token: str) -> str:
    return f"20{token[0:2]}-{token[2:4]}-{token[4:6]}"


if __name__ == "__main__":
    raise SystemExit(main())
