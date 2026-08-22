#!/usr/bin/env python3
"""Run the Vibe-Trading web server and supervised platform workers as one service.

A single process supervisor is intentional for the current DuckDB architecture:
the API, whole-market research worker, and lifecycle/learning worker share the
same persistent filesystem. If any child exits unexpectedly, the supervisor
terminates the others and exits so the hosting platform restarts the complete
service rather than serving stale or partially-updated state.
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
    market_worker = subprocess.Popen(
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
    lifecycle_worker = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "scripts" / "run_trading_platform_lifecycle_worker.py"),
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

    named_children = (
        ("market_worker", market_worker),
        ("lifecycle_worker", lifecycle_worker),
        ("web", web),
    )
    children = tuple(child for _, child in named_children)
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
            exited = [
                (name, child.poll())
                for name, child in named_children
                if child.poll() is not None
            ]
            if exited:
                name, code = exited[0]
                print(f"trading platform child {name} exited with code {code}; restarting service", flush=True)
                for child in children:
                    if child.poll() is None:
                        child.terminate()
                _wait(children, timeout=20.0)
                return int(code or 1)
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
