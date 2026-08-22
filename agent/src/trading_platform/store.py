"""Persistent trading-platform snapshots and append-only audit events.

The store contains research/platform state only. It has no broker dependency and
cannot place, cancel or modify orders. Secrets and raw broker credentials must
never be written here.
"""

from __future__ import annotations

import json
from pathlib import Path
import threading
from typing import Any

import duckdb

from .models import PlatformEvent, TradingDeskSnapshot

_SCHEMA_VERSION = "1"


class TradingPlatformStore:
    """Small DuckDB store for UI snapshots and audit/event history."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._connection = duckdb.connect(self.path)
        self._initialize()

    def __enter__(self) -> "TradingPlatformStore":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:  # type: ignore[no-untyped-def]
        del exc_type, exc, traceback
        self.close()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def save_snapshot(self, snapshot: TradingDeskSnapshot) -> str:
        payload = json.dumps(snapshot.model_dump(mode="json"), separators=(",", ":"), allow_nan=False)
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO platform_snapshots
                    (snapshot_id, created_at, environment, decision, schema_version, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    snapshot.snapshot_id,
                    snapshot.created_at,
                    snapshot.environment.value,
                    snapshot.decision.value,
                    snapshot.schema_version,
                    payload,
                ],
            )
        return snapshot.snapshot_id

    def latest_snapshot(self) -> TradingDeskSnapshot | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT payload_json
                FROM platform_snapshots
                ORDER BY created_at DESC, ingested_at DESC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        return TradingDeskSnapshot.model_validate(json.loads(str(row[0])))

    def snapshot_by_id(self, snapshot_id: str) -> TradingDeskSnapshot | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM platform_snapshots WHERE snapshot_id = ? LIMIT 1",
                [str(snapshot_id)],
            ).fetchone()
        if row is None:
            return None
        return TradingDeskSnapshot.model_validate(json.loads(str(row[0])))

    def append_event(self, event: PlatformEvent) -> str:
        payload = json.dumps(event.payload, separators=(",", ":"), allow_nan=False, default=str)
        system = json.dumps(event.system.model_dump(mode="json"), separators=(",", ":"), allow_nan=False)
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO platform_events
                    (event_id, occurred_at, event_type, environment, system_json, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    event.event_id,
                    event.occurred_at,
                    event.event_type,
                    event.environment.value,
                    system,
                    payload,
                ],
            )
        return event.event_id

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        if not 1 <= int(limit) <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT event_id, occurred_at, event_type, environment, system_json, payload_json
                FROM platform_events
                ORDER BY occurred_at DESC, ingested_at DESC
                LIMIT ?
                """,
                [int(limit)],
            ).fetchall()
        return [
            {
                "event_id": row[0],
                "occurred_at": row[1].isoformat() if hasattr(row[1], "isoformat") else str(row[1]),
                "event_type": row[2],
                "environment": row[3],
                "system": json.loads(str(row[4])),
                "payload": json.loads(str(row[5])),
            }
            for row in rows
        ]

    def counts(self) -> dict[str, int]:
        with self._lock:
            snapshots = int(self._connection.execute("SELECT COUNT(*) FROM platform_snapshots").fetchone()[0])
            events = int(self._connection.execute("SELECT COUNT(*) FROM platform_events").fetchone()[0])
        return {"snapshots": snapshots, "events": events}

    def _initialize(self) -> None:
        with self._lock:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS platform_meta (
                    key VARCHAR PRIMARY KEY,
                    value VARCHAR NOT NULL
                )
                """
            )
            row = self._connection.execute(
                "SELECT value FROM platform_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                self._connection.execute(
                    "INSERT INTO platform_meta VALUES ('schema_version', ?)",
                    [_SCHEMA_VERSION],
                )
            elif str(row[0]) != _SCHEMA_VERSION:
                raise RuntimeError(
                    f"unsupported trading-platform store schema {row[0]!r}; expected {_SCHEMA_VERSION}"
                )

            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS platform_snapshots (
                    snapshot_id VARCHAR PRIMARY KEY,
                    created_at TIMESTAMP NOT NULL,
                    environment VARCHAR NOT NULL,
                    decision VARCHAR NOT NULL,
                    schema_version INTEGER NOT NULL,
                    payload_json VARCHAR NOT NULL,
                    ingested_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS platform_events (
                    event_id VARCHAR PRIMARY KEY,
                    occurred_at TIMESTAMP NOT NULL,
                    event_type VARCHAR NOT NULL,
                    environment VARCHAR NOT NULL,
                    system_json VARCHAR NOT NULL,
                    payload_json VARCHAR NOT NULL,
                    ingested_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )


__all__ = ["TradingPlatformStore"]
