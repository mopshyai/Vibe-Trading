#!/usr/bin/env python3
"""Run proposal-only PAPER exits and retrospective learning as a sidecar loop.

This worker is intentionally separate from the whole-market scan loop. During an
open PAPER session it refreshes exact option exit inputs and evaluates exit
proposals. After the U.S. session it checkpoints mature candidate outcomes at
most once per ET calendar day. It never submits an order.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
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
from src.trading_platform.paper_lifecycle import fetch_alpaca_market_clock  # noqa: E402
from src.trading_platform.runtime_config import load_alpaca_runtime_config  # noqa: E402

UTC = timezone.utc
ET = ZoneInfo("America/New_York")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Trading Platform lifecycle/learning worker")
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--open-sleep", type=int, default=120)
    parser.add_argument("--closed-sleep", type=int, default=900)
    parser.add_argument("--command-timeout", type=int, default=300)
    parser.add_argument("--option-feed", choices=["opra", "indicative"], default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.open_sleep < 30 or args.closed_sleep < 60:
        raise ValueError("lifecycle worker sleep bounds are too aggressive")
    data_dir = args.data_dir or Path(
        os.environ.get("TRADING_PLATFORM_DATA_DIR")
        or (get_runtime_root() / "trading-platform")
    )
    data_dir.mkdir(parents=True, exist_ok=True)
    feed = args.option_feed or os.environ.get("TRADING_PLATFORM_OPTION_FEED", "opra").strip().lower()
    if feed not in {"opra", "indicative"}:
        raise ValueError("option feed must be opra or indicative")

    cycle = 0
    while True:
        cycle += 1
        result = run_lifecycle_cycle(
            data_dir=data_dir,
            cycle=cycle,
            option_feed=feed,
            command_timeout=args.command_timeout,
        )
        _atomic_json(data_dir / "lifecycle-heartbeat.json", result)
        print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
        if args.once:
            return 0
        sleep_seconds = args.open_sleep if bool(result.get("market_open")) else args.closed_sleep
        time.sleep(sleep_seconds)


def run_lifecycle_cycle(
    *,
    data_dir: Path,
    cycle: int,
    option_feed: str,
    command_timeout: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_at = (now or datetime.now(UTC)).astimezone(UTC)
    paths = _paths(data_dir)
    statuses: dict[str, Any] = {}

    market_open = False
    try:
        clock = fetch_alpaca_market_clock(load_alpaca_runtime_config())
        market_open = bool(clock.get("is_open"))
        statuses["market_clock"] = {"status": "ok", **clock}
    except Exception as exc:  # noqa: BLE001 - read failure is a fail-closed lifecycle state
        statuses["market_clock"] = {
            "status": "error",
            "reason": f"{type(exc).__name__}: {exc}",
        }

    if market_open:
        statuses["exit_state"] = _run(
            [
                "refresh_paper_exit_state.py",
                "--positions-output", str(paths["exit_positions"]),
                "--quotes-output", str(paths["exit_quotes"]),
                "--store", str(paths["platform_store"]),
                "--risk-json", str(paths["risk"]),
                "--feed", option_feed,
            ],
            timeout=command_timeout,
            acceptable_codes=(0, 2),
        )
        if statuses["exit_state"].get("status") == "ok" and paths["exit_positions"].exists() and paths["exit_quotes"].exists():
            statuses["exit_evaluation"] = _run(
                [
                    "evaluate_paper_exits.py",
                    "--positions-json", str(paths["exit_positions"]),
                    "--quotes-json", str(paths["exit_quotes"]),
                    "--output", str(paths["exit_report"]),
                    "--state", str(paths["exit_watermarks"]),
                    "--platform-store", str(paths["platform_store"]),
                ],
                timeout=command_timeout,
                acceptable_codes=(0, 2),
            )
        else:
            statuses["exit_evaluation"] = {
                "status": "skipped",
                "reason": "fresh_exit_state_unavailable",
            }
    else:
        statuses["exit_state"] = {"status": "skipped", "reason": "market_closed_or_clock_unavailable"}
        statuses["exit_evaluation"] = {"status": "skipped", "reason": "market_closed_or_clock_unavailable"}

    if _should_checkpoint_attribution(observed_at, paths["attribution"]):
        statuses["attribution"] = _run(
            [
                "checkpoint_candidate_outcomes.py",
                "--platform-store", str(paths["platform_store"]),
                "--research-store", str(paths["research_store"]),
                "--output", str(paths["attribution"]),
                "--as-of", observed_at.isoformat(),
            ],
            timeout=command_timeout,
        )
    else:
        statuses["attribution"] = {"status": "cached_or_session_not_finished"}

    return {
        "schema_version": 1,
        "cycle": cycle,
        "observed_at": observed_at.isoformat(),
        "market_open": market_open,
        "option_feed": option_feed,
        "statuses": statuses,
        "exit_report": _json(paths["exit_report"]) if paths["exit_report"].exists() else None,
        "attribution": _json(paths["attribution"]) if paths["attribution"].exists() else None,
        "automatic_entry_submission": False,
        "automatic_exit_submission": False,
        "broker_mutation": False,
    }


def _should_checkpoint_attribution(now: datetime, path: Path) -> bool:
    local = now.astimezone(ET)
    # Wait until well after the core session; on non-trading days this simply
    # allows one daily checkpoint, which is useful when historical data lands late.
    if local.hour < 17:
        return False
    if not path.exists():
        return True
    payload = _json(path)
    if isinstance(payload, Mapping):
        stamp = payload.get("as_of") or payload.get("generated_at")
        if stamp:
            try:
                prior = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
                if prior.tzinfo is not None and prior.astimezone(ET).date() == local.date():
                    return False
            except ValueError:
                pass
    try:
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return True
    return modified.astimezone(ET).date() != local.date()


def _paths(data_dir: Path) -> dict[str, Path]:
    return {
        "platform_store": data_dir / "trading-platform.duckdb",
        "research_store": data_dir / "options-research.duckdb",
        "risk": data_dir / "account-risk.json",
        "exit_positions": data_dir / "paper-exit-positions.json",
        "exit_quotes": data_dir / "paper-exit-quotes.json",
        "exit_report": data_dir / "paper-exit-report.json",
        "exit_watermarks": data_dir / "paper-exit-watermarks.json",
        "attribution": data_dir / "candidate-attribution.json",
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
        return {
            "status": "error",
            "reason": "timeout",
            "seconds": timeout,
            "stderr": _tail(exc.stderr),
        }
    except OSError as exc:
        return {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}
    return {
        "status": "ok" if completed.returncode in acceptable_codes else "error",
        "returncode": completed.returncode,
        "stdout_tail": _tail(completed.stdout),
        "stderr_tail": _tail(completed.stderr),
    }


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


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
