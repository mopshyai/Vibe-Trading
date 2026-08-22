#!/usr/bin/env python3
"""Run web, Trading Desk worker and whole-market equity refresher together.

A single process supervisor is intentional for the current DuckDB architecture:
the API, research worker and bulk equity refresh loop share one persistent
filesystem. If any child exits, the supervisor terminates the others and exits so
the hosting platform restarts the complete stack rather than serving stale state.
"""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    port = str(os.environ.get("PORT") or "8899").strip()
    data_dir = str(
        os.environ.get("TRADING_PLATFORM_DATA_DIR")
        or (Path.home() / ".vibe-trading" / "trading-platform")
    )
    worker = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "scripts" / "run_trading_platform_worker.py"),
            "--data-dir",
            data_dir,
            "--force-full",
        ],
        cwd=ROOT,
        env=os.environ.copy(),
    )
    equity_refresh = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "scripts" / "run_equity_refresh_loop.py"),
            "--data-dir",
            data_dir,
        ],
        cwd=ROOT,
        env=os.environ.copy(),
    )
    web = subprocess.Popen(
        [
            "vibe-trading",
            "serve",
            "--host",
            "0.0.0.0",
            "--port",
            port,
        ],
        cwd=ROOT,
        env=os.environ.copy(),
    )

    children = (worker, equity_refresh, web)
    stopping = False

    def stop(signum: int, _frame: object) -> None:
        nonlocal stopping
        if stopping:
            return
        stopping = True
        print(f"received signal {signum}; stopping trading platform children", flush=True)
        for child in children:
            if child.poll() is None:
                child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        while True:
            exit_codes = [child.poll() for child in children]
            if any(code is not None for code in exit_codes):
                for child in children:
                    if child.poll() is None:
                        child.terminate()
                _wait(children, timeout=20.0)
                for code in exit_codes:
                    if code is not None:
                        return int(code or 1)
                return 1
            time.sleep(1.0)
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        _wait(children, timeout=10.0)
        for child in children:
            if child.poll() is None:
                child.kill()


def _wait(children: tuple[subprocess.Popen, ...], *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    for child in children:
        remaining = max(0.0, deadline - time.monotonic())
        if child.poll() is not None:
            continue
        try:
            child.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
