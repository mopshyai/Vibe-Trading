#!/usr/bin/env python3
"""Keep the current edge of the whole-market daily-bar store refreshed.

Runs beside the main Trading Desk worker. It waits for the daily universe
artifact, refreshes recent split-adjusted Alpaca bars once per New York market
calendar date, and retries failed refreshes without marking the main worker as
healthy. No broker mutation is possible from this process.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
UTC = timezone.utc
ET = ZoneInfo("America/New_York")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh whole-market daily equity bars persistently")
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--feed", choices=["sip", "iex"], default=None)
    parser.add_argument("--retry-seconds", type=int, default=300)
    parser.add_argument("--idle-seconds", type=int, default=1800)
    parser.add_argument("--command-timeout", type=int, default=900)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    feed = args.feed or os.environ.get("TRADING_PLATFORM_STOCK_FEED", "sip").strip().lower()
    if feed not in {"sip", "iex"}:
        raise ValueError("TRADING_PLATFORM_STOCK_FEED must be sip or iex")

    universe = args.data_dir / "us-universe.json"
    store = args.data_dir / "options-research.duckdb"
    manifest = args.data_dir / "data-plane-manifest.json"
    metadata = args.data_dir / "equity-daily-refresh.json"
    last_success_date: str | None = None

    while True:
        current_date = datetime.now(ET).date().isoformat()
        if last_success_date == current_date:
            time.sleep(max(30, args.idle_seconds))
            continue
        if not universe.exists() or not _has_symbols(universe):
            time.sleep(max(30, min(args.retry_seconds, 300)))
            continue

        command = [
            sys.executable,
            str(SCRIPTS / "refresh_current_equity_bars.py"),
            "--symbols-file", str(universe),
            "--store", str(store),
            "--output", str(metadata),
            "--manifest", str(manifest),
            "--feed", feed,
        ]
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=os.environ.copy(),
            text=True,
            capture_output=True,
            timeout=args.command_timeout,
            check=False,
        )
        if completed.returncode == 0:
            last_success_date = current_date
            print(completed.stdout.strip(), flush=True)
            time.sleep(max(30, args.idle_seconds))
        else:
            print(
                json.dumps(
                    {
                        "status": "error",
                        "component": "equity_daily_bars",
                        "returncode": completed.returncode,
                        "stderr_tail": completed.stderr[-1200:],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            time.sleep(max(30, args.retry_seconds))


def _has_symbols(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    values = payload.get("symbols") if isinstance(payload, dict) else payload
    return isinstance(values, list) and len(values) > 0


if __name__ == "__main__":
    raise SystemExit(main())
