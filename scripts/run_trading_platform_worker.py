#!/usr/bin/env python3
"""Continuously refresh the supervised personal Trading Desk.

This is the hosted orchestration loop. It composes existing narrow CLIs rather
than duplicating their trading/research rules:

1. refresh authoritative XNYS calendar + U.S. listing universe (daily),
2. run a whole-market/phase-aware chart pass from the point-in-time store,
3. refresh focused OPRA/current option data and catalyst news,
4. rerun the analysis with those current focused inputs,
5. read the Alpaca PAPER account and calculate account-level risk,
6. apply personal EV/walk-forward/regime/contract/risk gates,
7. publish the durable Trading Desk snapshot and complete candidate journal,
8. prepare a current dry-run PAPER proposal when a candidate is truly ready,
9. reconcile any previously submitted PAPER orders,
10. publish a read-only operational preflight + heartbeat.

The worker NEVER submits a new broker order. New paper mutations remain behind
``run_paper_option_lifecycle.py --submit-paper --confirm-paper-submit``. The
worker only prepares a dry-run proposal and performs read-only reconciliation.
Historical Databento backfills are also intentionally NOT automatic because they
can incur provider charges.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
AGENT = ROOT / "agent"
if str(AGENT) not in sys.path:
    sys.path.insert(0, str(AGENT))

from src.config.paths import get_runtime_root  # noqa: E402

UTC = timezone.utc
ET = ZoneInfo("America/New_York")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the hosted Trading Desk worker")
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--once", action="store_true", help="Run exactly one orchestration cycle")
    parser.add_argument("--force-full", action="store_true", help="Force the first analysis pass to be a full scan")
    parser.add_argument("--option-feed", choices=["opra", "indicative"], default=None)
    parser.add_argument("--sleep-floor", type=int, default=60)
    parser.add_argument("--sleep-ceiling", type=int, default=900)
    parser.add_argument("--command-timeout", type=int, default=900)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.sleep_floor < 1 or args.sleep_ceiling < args.sleep_floor:
        raise ValueError("invalid worker sleep bounds")
    if args.command_timeout < 10:
        raise ValueError("command-timeout must be at least 10 seconds")

    data_dir = args.data_dir or Path(os.environ.get("TRADING_PLATFORM_DATA_DIR") or (get_runtime_root() / "trading-platform"))
    data_dir.mkdir(parents=True, exist_ok=True)
    option_feed = args.option_feed or os.environ.get("TRADING_PLATFORM_OPTION_FEED", "opra").strip().lower()
    if option_feed not in {"opra", "indicative"}:
        raise ValueError("TRADING_PLATFORM_OPTION_FEED must be opra or indicative")

    cycle_number = 0
    first = True
    while True:
        cycle_number += 1
        started = datetime.now(UTC)
        result = run_worker_cycle(
            data_dir=data_dir,
            cycle_number=cycle_number,
            option_feed=option_feed,
            force_full=args.force_full and first,
            command_timeout=args.command_timeout,
        )
        first = False
        _atomic_json(data_dir / "worker-heartbeat.json", result)
        print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
        if args.once:
            return 0 if result.get("platform_snapshot_published") else 2

        suggested = _suggested_sleep(data_dir / "analysis.json")
        sleep_seconds = min(args.sleep_ceiling, max(args.sleep_floor, suggested))
        elapsed = max(0.0, (datetime.now(UTC) - started).total_seconds())
        result["next_cycle_seconds"] = sleep_seconds
        result["last_cycle_elapsed_seconds"] = round(elapsed, 3)
        _atomic_json(data_dir / "worker-heartbeat.json", result)
        time.sleep(sleep_seconds)


def run_worker_cycle(
    *,
    data_dir: Path,
    cycle_number: int,
    option_feed: str,
    force_full: bool,
    command_timeout: int,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    paths = _paths(data_dir)
    statuses: dict[str, Any] = {}

    if _needs_daily_refresh(paths["calendar"], now):
        start = (now.astimezone(ET).date() - timedelta(days=7)).isoformat()
        end = (now.astimezone(ET).date() + timedelta(days=45)).isoformat()
        statuses["calendar"] = _run(
            [
                "generate_us_market_calendar.py",
                "--start", start,
                "--end", end,
                "--output", str(paths["calendar"]),
                "--manifest", str(paths["manifest"]),
            ],
            timeout=command_timeout,
        )
    else:
        statuses["calendar"] = {"status": "cached"}

    if _needs_daily_refresh(paths["universe"], now):
        statuses["universe"] = _run(
            [
                "refresh_us_universe.py",
                "--output", str(paths["universe"]),
                "--manifest", str(paths["manifest"]),
            ],
            timeout=command_timeout,
        )
    else:
        statuses["universe"] = {"status": "cached"}

    _ensure_json(paths["universe"], {"symbols": []})
    _ensure_json(paths["options"], {})
    _ensure_json(paths["catalysts"], {})

    analysis_cmd = [
        "run_continuous_options_analysis.py",
        "--store", str(paths["research_store"]),
        "--state", str(paths["analysis_state"]),
        "--symbols-file", str(paths["universe"]),
        "--options-json", str(paths["options"]),
        "--catalysts-json", str(paths["catalysts"]),
        "--session-calendar-json", str(paths["calendar"]),
        "--require-authoritative-calendar",
        "--output", str(paths["analysis"]),
    ]
    if force_full:
        analysis_cmd.append("--force-full")
    statuses["analysis_pass_1"] = _run(analysis_cmd, timeout=command_timeout)

    if paths["analysis"].exists():
        statuses["options_refresh"] = _run(
            [
                "refresh_focused_options.py",
                "--focus-json", str(paths["analysis"]),
                "--output", str(paths["options"]),
                "--manifest", str(paths["manifest"]),
                "--feed", option_feed,
            ],
            timeout=command_timeout,
        )
        statuses["catalyst_refresh"] = _run(
            [
                "refresh_catalysts.py",
                "--focus-json", str(paths["analysis"]),
                "--store", str(paths["catalyst_store"]),
                "--output", str(paths["catalysts"]),
                "--manifest", str(paths["manifest"]),
            ],
            timeout=command_timeout,
        )
        statuses["analysis_pass_2"] = _run(analysis_cmd, timeout=command_timeout)
    else:
        statuses["options_refresh"] = {"status": "skipped", "reason": "analysis_output_unavailable"}
        statuses["catalyst_refresh"] = {"status": "skipped", "reason": "analysis_output_unavailable"}
        statuses["analysis_pass_2"] = {"status": "skipped", "reason": "analysis_output_unavailable"}

    statuses["account_risk"] = _run(
        [
            "assess_account_risk.py",
            "--alpaca-account",
            "--output", str(paths["risk"]),
            "--summary-output", str(paths["risk_summary"]),
        ],
        timeout=command_timeout,
        acceptable_codes=(0, 2),
    )
    if not paths["risk_summary"].exists():
        _atomic_json(
            paths["risk_summary"],
            {
                "account_equity_usd": None,
                "open_premium_risk_usd": None,
                "open_premium_risk_pct": None,
                "daily_realized_pnl_usd": None,
                "weekly_realized_pnl_usd": None,
                "max_drawdown_pct": None,
                "positions": 0,
                "trading_blocked": True,
                "blocking_reasons": ["account_risk_unavailable"],
                "greeks": {},
                "concentration": {},
            },
        )
    if not paths["risk"].exists():
        _atomic_json(
            paths["risk"],
            {
                "approved": False,
                "trading_blocked": True,
                "decision": "ACCOUNT_RISK_BLOCKED",
                "blocking_reasons": ["account_risk_unavailable"],
            },
        )

    if paths["analysis"].exists():
        personal_cmd = [
            "run_personal_options_assistant.py",
            "--analysis-json", str(paths["analysis"]),
            "--alpaca-account",
            "--output", str(paths["personal_cycle"]),
        ]
        if paths["ev"].exists():
            personal_cmd += ["--ev-json", str(paths["ev"])]
        if paths["walkforward"].exists():
            personal_cmd += ["--walkforward-json", str(paths["walkforward"])]
        if paths["dashboard"].exists():
            personal_cmd += ["--previous-dashboard", str(paths["dashboard"])]
        statuses["personal"] = _run(personal_cmd, timeout=command_timeout)
    else:
        statuses["personal"] = {"status": "skipped", "reason": "analysis_output_unavailable"}

    if not paths["personal_cycle"].exists():
        _atomic_json(paths["personal_cycle"], _blocked_cycle(paths["analysis"], "personal_runtime_unavailable"))
    else:
        cycle = _json(paths["personal_cycle"])
        if isinstance(cycle, Mapping) and isinstance(cycle.get("dashboard"), Mapping):
            _atomic_json(paths["dashboard"], dict(cycle["dashboard"]))

    publish_cmd = [
        "publish_trading_desk.py",
        "--cycle-json", str(paths["personal_cycle"]),
        "--store", str(paths["platform_store"]),
        "--environment", "paper",
        "--risk-json", str(paths["risk_summary"]),
    ]
    if paths["manifest"].exists():
        publish_cmd += ["--health-json", str(paths["manifest"])]
    commit_sha = os.environ.get("RENDER_GIT_COMMIT") or os.environ.get("GIT_COMMIT_SHA")
    if commit_sha:
        publish_cmd += ["--commit-sha", commit_sha]
    statuses["publish"] = _run(publish_cmd, timeout=command_timeout)
    platform_snapshot_published = statuses["publish"].get("status") == "ok"

    proposal_bundle = _build_execution_bundle(paths["personal_cycle"], paths["risk"])
    if proposal_bundle is not None:
        _atomic_json(paths["execution_input"], proposal_bundle)
        statuses["paper_proposal"] = _run(
            [
                "run_paper_option_lifecycle.py",
                "--input-json", str(paths["execution_input"]),
                "--store", str(paths["platform_store"]),
                "--option-feed", "opra",
                "--output", str(paths["paper_proposal"]),
            ],
            timeout=command_timeout,
            acceptable_codes=(0, 2),
        )
    else:
        _atomic_json(
            paths["paper_proposal"],
            {
                "ready": False,
                "submitted": False,
                "decision": "NO_PAPER_PROPOSAL",
                "reasons": ["no_trade_ready_candidate_or_account_risk_blocked"],
                "observed_at": now.isoformat(),
            },
        )
        statuses["paper_proposal"] = {"status": "skipped", "reason": "no_trade_ready_candidate_or_account_risk_blocked"}

    statuses["paper_reconcile"] = _run(
        [
            "run_paper_option_lifecycle.py",
            "--sync-only",
            "--store", str(paths["platform_store"]),
            "--output", str(paths["paper_sync"]),
        ],
        timeout=command_timeout,
        acceptable_codes=(0, 2),
    )

    preflight_cmd = [
        "trading_platform_preflight.py",
        "--store", str(paths["platform_store"]),
        "--output", str(paths["preflight"]),
    ]
    contract = _proposal_contract(paths["paper_proposal"])
    if contract:
        preflight_cmd += ["--contract", contract]
    if paths["execution_input"].exists():
        preflight_cmd += ["--execution-input-json", str(paths["execution_input"])]
    statuses["preflight"] = _run(preflight_cmd, timeout=command_timeout, acceptable_codes=(0, 2))

    return {
        "schema_version": 1,
        "cycle": cycle_number,
        "observed_at": datetime.now(UTC).isoformat(),
        "platform_snapshot_published": platform_snapshot_published,
        "option_feed": option_feed,
        "data_dir": str(data_dir),
        "statuses": statuses,
        "preflight": _json(paths["preflight"]) if paths["preflight"].exists() else None,
        "paper_proposal": _json(paths["paper_proposal"]) if paths["paper_proposal"].exists() else None,
        "historical_backfill_automatic": False,
        "new_paper_submission_automatic": False,
    }


def _paths(data_dir: Path) -> dict[str, Path]:
    return {
        "research_store": data_dir / "options-research.duckdb",
        "platform_store": data_dir / "trading-platform.duckdb",
        "catalyst_store": data_dir / "catalysts.duckdb",
        "manifest": data_dir / "data-plane-manifest.json",
        "calendar": data_dir / "xnys-calendar.json",
        "universe": data_dir / "us-universe.json",
        "analysis_state": data_dir / "options-analysis-state.json",
        "analysis": data_dir / "analysis.json",
        "options": data_dir / "current-options.json",
        "catalysts": data_dir / "current-catalysts.json",
        "risk": data_dir / "account-risk.json",
        "risk_summary": data_dir / "account-risk-summary.json",
        "personal_cycle": data_dir / "personal-cycle.json",
        "dashboard": data_dir / "previous-dashboard.json",
        "ev": data_dir / "ev-reports.json",
        "walkforward": data_dir / "walkforward-reports.json",
        "execution_input": data_dir / "paper-execution-input.json",
        "paper_proposal": data_dir / "paper-proposal.json",
        "paper_sync": data_dir / "paper-sync.json",
        "preflight": data_dir / "preflight.json",
    }


def _build_execution_bundle(personal_path: Path, risk_path: Path) -> dict[str, Any] | None:
    cycle = _json(personal_path)
    risk = _json(risk_path)
    if not isinstance(cycle, Mapping) or not isinstance(risk, Mapping) or not bool(risk.get("approved")):
        return None
    personal = cycle.get("personal") if isinstance(cycle.get("personal"), Mapping) else {}
    candidates = personal.get("candidates") if isinstance(personal.get("candidates"), list) else []
    for row in candidates:
        if not isinstance(row, Mapping) or row.get("decision") != "TRADE_READY_RESEARCH":
            continue
        ev = row.get("empirical_ev") if isinstance(row.get("empirical_ev"), Mapping) else {}
        walk = row.get("walk_forward") if isinstance(row.get("walk_forward"), Mapping) else {}
        if not ev or not walk:
            continue
        return {
            "candidate": dict(row),
            "ev_report": dict(ev),
            "walk_forward_report": dict(walk),
            "risk_report": dict(risk),
        }
    return None


def _blocked_cycle(analysis_path: Path, reason: str) -> dict[str, Any]:
    market = _json(analysis_path) if analysis_path.exists() else {}
    return {
        "mode": "continuous_personal_options_research",
        "market": market if isinstance(market, Mapping) else {},
        "personal": {
            "mode": "personal_options_research",
            "decision": "NO_TRADE",
            "trade_ready_count": 0,
            "watch_count": 0,
            "candidate_count_evaluated": 0,
            "candidates": [],
            "journal_candidates": [],
        },
        "dashboard": {
            "decision": "NO_TRADE",
            "headline": "NO TRADE — personal/account runtime unavailable",
            "funnel": {},
            "cards": [],
            "disclaimer": "Fail-closed runtime state; no broker order is eligible.",
        },
        "alert": {"should_alert": False},
        "execution": {
            "mode": "supervised_paper",
            "status": "BLOCKED_RUNTIME",
            "eligible_candidates": 0,
            "automatic_submission": False,
            "paper_submit_confirmation_required": True,
            "runtime_revalidation_required": True,
            "reasons": [reason],
        },
        "warnings": [reason],
    }


def _run(
    args: Sequence[str],
    *,
    timeout: int,
    acceptable_codes: tuple[int, ...] = (0,),
) -> dict[str, Any]:
    command = [sys.executable, str(SCRIPTS / args[0]), *args[1:]]
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired as exc:
        return {"status": "error", "reason": "timeout", "seconds": timeout, "stderr": _tail(exc.stderr)}
    except OSError as exc:
        return {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}
    status = "ok" if completed.returncode in acceptable_codes else "error"
    return {
        "status": status,
        "returncode": completed.returncode,
        "stdout_tail": _tail(completed.stdout),
        "stderr_tail": _tail(completed.stderr),
    }


def _needs_daily_refresh(path: Path, now: datetime) -> bool:
    if not path.exists():
        return True
    payload = _json(path)
    if isinstance(payload, Mapping):
        observed = str(payload.get("observed_at") or "").strip()
        if observed:
            try:
                dt = datetime.fromisoformat(observed.replace("Z", "+00:00"))
                if dt.tzinfo is not None:
                    return dt.astimezone(ET).date() != now.astimezone(ET).date()
            except ValueError:
                pass
    try:
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return True
    return modified.astimezone(ET).date() != now.astimezone(ET).date()


def _suggested_sleep(path: Path) -> int:
    payload = _json(path) if path.exists() else {}
    if isinstance(payload, Mapping):
        plan = payload.get("plan") if isinstance(payload.get("plan"), Mapping) else {}
        try:
            value = int(plan.get("next_check_seconds") or 300)
        except (TypeError, ValueError, OverflowError):
            value = 300
        return max(1, value)
    return 300


def _proposal_contract(path: Path) -> str:
    payload = _json(path) if path.exists() else {}
    if not isinstance(payload, Mapping):
        return ""
    contract = payload.get("contract_symbol")
    if not contract and isinstance(payload.get("order"), Mapping):
        contract = payload["order"].get("symbol")
    return str(contract or "").strip().upper()


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _ensure_json(path: Path, payload: object) -> None:
    if not path.exists():
        _atomic_json(path, payload)


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False, default=str) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _tail(value: object, limit: int = 1200) -> str:
    if value is None:
        return ""
    text = value.decode(errors="replace") if isinstance(value, bytes) else str(value)
    return text[-limit:]


if __name__ == "__main__":
    raise SystemExit(main())
