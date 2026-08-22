#!/usr/bin/env python3
"""Run the Vibe-Trading web server and Trading Desk worker as one service.

A single process supervisor is intentional for the current DuckDB architecture:
the API and continuous worker share the same persistent filesystem. If either
child exits, this supervisor terminates the other and exits so the hosting
platform can restart the complete service rather than serving a stale desk.
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

    children = (worker, web)
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
            worker_code = worker.poll()
            web_code = web.poll()
            if worker_code is not None or web_code is not None:
                for child in children:
                    if child.poll() is None:
                        child.terminate()
                _wait(children, timeout=20.0)
                if web_code is not None:
                    return int(web_code)
                return int(worker_code or 1)
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
