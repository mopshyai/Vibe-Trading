#!/usr/bin/env python3
"""Hosted wrapper that sequences whole-market refresh with each decision cycle.

The underlying worker already owns calendar, focused options, catalysts, evidence,
risk, desk publication and paper reconciliation. This wrapper adds the missing
whole-market current-edge step *in the same process* before those stages:

    universe refresh -> bulk daily equity refresh -> normal decision cycle

Keeping those manifest writers sequential avoids a read/modify/write race between
independent processes while preserving the reusable base worker for local use.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import run_trading_platform_worker as base  # noqa: E402

UTC = timezone.utc
_ORIGINAL_CYCLE = base.run_worker_cycle


def hosted_cycle(
    *,
    data_dir: Path,
    cycle_number: int,
    option_feed: str,
    force_full: bool,
    command_timeout: int,
):
    now = datetime.now(UTC)
    data_dir.mkdir(parents=True, exist_ok=True)
    universe = data_dir / "us-universe.json"
    manifest = data_dir / "data-plane-manifest.json"
    research_store = data_dir / "options-research.duckdb"
    equity_refresh = data_dir / "equity-daily-refresh.json"

    # Refresh the symbol directory first so the equity call and subsequent chart
    # scan operate on the same daily universe. The base worker observes the fresh
    # artifact and will not repeat this refresh in the same cycle.
    if base._needs_daily_refresh(universe, now):  # noqa: SLF001 - deliberate orchestration reuse
        base._run(  # noqa: SLF001
            [
                "refresh_us_universe.py",
                "--output", str(universe),
                "--manifest", str(manifest),
            ],
            timeout=command_timeout,
        )

    if universe.exists() and base._needs_daily_refresh(equity_refresh, now):  # noqa: SLF001
        stock_feed = os.environ.get("TRADING_PLATFORM_STOCK_FEED", "sip").strip().lower()
        if stock_feed not in {"sip", "iex"}:
            stock_feed = "sip"
        # A failed refresh records equity_daily_bars=error in the shared manifest.
        # The base cycle then publishes a blocked/degraded Trading Desk rather
        # than reusing a prior healthy whole-market state.
        base._run(  # noqa: SLF001
            [
                "refresh_current_equity_bars.py",
                "--symbols-file", str(universe),
                "--store", str(research_store),
                "--output", str(equity_refresh),
                "--manifest", str(manifest),
                "--feed", stock_feed,
            ],
            timeout=command_timeout,
            acceptable_codes=(0, 2),
        )

    return _ORIGINAL_CYCLE(
        data_dir=data_dir,
        cycle_number=cycle_number,
        option_feed=option_feed,
        force_full=force_full,
        command_timeout=command_timeout,
    )


def main() -> int:
    base.run_worker_cycle = hosted_cycle
    return base.main()


if __name__ == "__main__":
    raise SystemExit(main())
