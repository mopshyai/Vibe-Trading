"""Read-only Trading Desk HTTP routes.

The routes expose the latest durable platform snapshot, audit events, candidate
journal and retrospective attribution. They do not accept order payloads and do
not import any broker mutation functions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import Depends, FastAPI, Query

from src.config.paths import get_runtime_root
from src.trading_platform import TradingPlatformStore, build_attribution_report

AuthDep = Callable[..., Awaitable[Any] | Any]


def _platform_store_path() -> Path:
    return get_runtime_root() / "trading-platform.duckdb"


def register_trading_platform_routes(
    app: FastAPI,
    require_auth: AuthDep | None = None,
) -> None:
    """Mount the broker-write-free Trading Desk endpoints."""
    if require_auth is None:
        import sys as _sys

        host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
        if host is None:  # pragma: no cover
            raise RuntimeError(
                "register_trading_platform_routes: api_server module not in sys.modules; "
                "pass require_auth explicitly"
            )
        require_auth = host.require_auth

    @app.get("/api/trading-desk", dependencies=[Depends(require_auth)])
    async def get_trading_desk() -> dict[str, Any]:
        path = _platform_store_path()
        if not path.exists():
            return {
                "status": "ok",
                "snapshot": None,
                "counts": {"snapshots": 0, "events": 0, "journal": 0},
                "store": "not_initialized",
            }
        with TradingPlatformStore(path) as store:
            snapshot = store.latest_snapshot()
            return {
                "status": "ok",
                "snapshot": snapshot.model_dump(mode="json") if snapshot else None,
                "counts": store.counts(),
                "store": "ready",
            }

    @app.get("/api/trading-desk/events", dependencies=[Depends(require_auth)])
    async def get_trading_desk_events(
        limit: int = Query(50, ge=1, le=250),
    ) -> dict[str, Any]:
        path = _platform_store_path()
        if not path.exists():
            return {"status": "ok", "events": []}
        with TradingPlatformStore(path) as store:
            return {"status": "ok", "events": store.recent_events(limit)}

    @app.get("/api/trading-desk/journal", dependencies=[Depends(require_auth)])
    async def get_trading_desk_journal(
        limit: int = Query(100, ge=1, le=500),
        symbol: str | None = Query(default=None, max_length=32),
        contract_symbol: str | None = Query(default=None, max_length=64),
    ) -> dict[str, Any]:
        path = _platform_store_path()
        if not path.exists():
            return {"status": "ok", "journal": []}
        with TradingPlatformStore(path) as store:
            return {
                "status": "ok",
                "journal": store.recent_journal(
                    limit,
                    symbol=symbol,
                    contract_symbol=contract_symbol,
                ),
            }

    @app.get("/api/trading-desk/attribution", dependencies=[Depends(require_auth)])
    async def get_trading_desk_attribution(
        limit: int = Query(2000, ge=1, le=2000),
    ) -> dict[str, Any]:
        path = _platform_store_path()
        if not path.exists():
            return {
                "status": "ok",
                "attribution": build_attribution_report([]),
            }
        with TradingPlatformStore(path) as store:
            return {
                "status": "ok",
                "attribution": build_attribution_report(store.recent_journal(limit)),
            }


__all__ = ["register_trading_platform_routes"]
