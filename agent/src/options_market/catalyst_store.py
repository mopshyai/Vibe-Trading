"""Persistent point-in-time catalyst/news event store.

The store preserves publication availability timestamps and source revisions.
Research queries are explicitly constrained to ``published_at <= as_of`` so a
historical replay cannot see a later headline or filing.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import threading
from typing import Any, Iterable, Mapping

import duckdb
import pandas as pd

from .catalyst import CatalystEvent

_SCHEMA_VERSION = "1"


class CatalystEventStore:
    """DuckDB-backed append-only catalyst event store."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._connection = duckdb.connect(self.path)
        self._initialize()

    def __enter__(self) -> "CatalystEventStore":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:  # type: ignore[no-untyped-def]
        del exc_type, exc, traceback
        self.close()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def ingest(self, rows: Iterable[Mapping[str, Any]]) -> int:
        normalized = [_normalize_row(row) for row in rows]
        normalized = [row for row in normalized if row is not None]
        if not normalized:
            return 0
        frame = pd.DataFrame(normalized)
        with self._lock:
            self._connection.register("_incoming_catalysts", frame)
            try:
                self._connection.execute(
                    """
                    INSERT INTO catalyst_events (
                        external_id, symbol, event_type, published_at, updated_at,
                        direction, magnitude, source_quality, novelty,
                        volatility_risk, headline, source, url, metadata_json,
                        ingested_at
                    )
                    SELECT
                        external_id, symbol, event_type, published_at, updated_at,
                        direction, magnitude, source_quality, novelty,
                        volatility_risk, headline, source, url, metadata_json,
                        CURRENT_TIMESTAMP
                    FROM _incoming_catalysts
                    """
                )
            finally:
                self._connection.unregister("_incoming_catalysts")
        return len(normalized)

    def events_asof(
        self,
        *,
        as_of: object,
        symbols: Iterable[str] | None = None,
        start: object | None = None,
        max_rows: int = 5000,
    ) -> list[dict[str, Any]]:
        if not 1 <= int(max_rows) <= 100_000:
            raise ValueError("max_rows must be between 1 and 100000")
        as_of_ts = _timestamp(as_of, "as_of")
        start_ts = _timestamp(start, "start") if start is not None else None
        if start_ts is not None and start_ts > as_of_ts:
            raise ValueError("start cannot be after as_of")

        clauses = ["published_at <= ?"]
        params: list[Any] = [as_of_ts]
        if start_ts is not None:
            clauses.append("published_at >= ?")
            params.append(start_ts)
        normalized_symbols = _symbols(symbols or [])
        if normalized_symbols:
            placeholders = ",".join("?" for _ in normalized_symbols)
            clauses.append(f"symbol IN ({placeholders})")
            params.extend(normalized_symbols)
        params.append(int(max_rows))
        where = " AND ".join(clauses)

        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT
                    external_id, symbol, event_type, published_at, updated_at,
                    direction, magnitude, source_quality, novelty,
                    volatility_risk, headline, source, url, metadata_json,
                    ingested_at
                FROM (
                    SELECT *,
                        ROW_NUMBER() OVER (
                            PARTITION BY source, external_id, symbol
                            ORDER BY COALESCE(updated_at, published_at) DESC,
                                     ingested_at DESC
                        ) AS revision_rank
                    FROM catalyst_events
                    WHERE {where}
                ) revisions
                WHERE revision_rank = 1
                ORDER BY published_at DESC, symbol
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def catalyst_events_asof(
        self,
        *,
        as_of: object,
        symbols: Iterable[str] | None = None,
        start: object | None = None,
        max_rows: int = 5000,
    ) -> list[CatalystEvent]:
        rows = self.events_asof(
            as_of=as_of,
            symbols=symbols,
            start=start,
            max_rows=max_rows,
        )
        return [
            CatalystEvent.from_mapping(
                {
                    "symbol": row["symbol"],
                    "event_type": row["event_type"],
                    "published_at": row["published_at"],
                    "direction": row["direction"],
                    "magnitude": row["magnitude"],
                    "source_quality": row["source_quality"],
                    "novelty": row["novelty"],
                    "volatility_risk": row["volatility_risk"],
                    "headline": row["headline"],
                    "source": row["source"],
                }
            )
            for row in rows
        ]

    def count(self) -> int:
        with self._lock:
            return int(self._connection.execute("SELECT COUNT(*) FROM catalyst_events").fetchone()[0])

    def _initialize(self) -> None:
        with self._lock:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS catalyst_meta (
                    key VARCHAR PRIMARY KEY,
                    value VARCHAR NOT NULL
                )
                """
            )
            row = self._connection.execute(
                "SELECT value FROM catalyst_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                self._connection.execute(
                    "INSERT INTO catalyst_meta VALUES ('schema_version', ?)",
                    [_SCHEMA_VERSION],
                )
            elif str(row[0]) != _SCHEMA_VERSION:
                raise RuntimeError(
                    f"unsupported catalyst-store schema {row[0]!r}; expected {_SCHEMA_VERSION}"
                )

            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS catalyst_events (
                    external_id VARCHAR NOT NULL,
                    symbol VARCHAR NOT NULL,
                    event_type VARCHAR NOT NULL,
                    published_at TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP,
                    direction VARCHAR NOT NULL,
                    magnitude DOUBLE NOT NULL,
                    source_quality DOUBLE NOT NULL,
                    novelty DOUBLE NOT NULL,
                    volatility_risk DOUBLE NOT NULL,
                    headline VARCHAR,
                    source VARCHAR NOT NULL,
                    url VARCHAR,
                    metadata_json VARCHAR NOT NULL,
                    ingested_at TIMESTAMP NOT NULL
                )
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS catalyst_symbol_time_idx ON catalyst_events(symbol, published_at)"
            )


def _normalize_row(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    try:
        event = CatalystEvent.from_mapping(raw)
    except ValueError:
        return None
    source = str(raw.get("source") or event.source or "unknown").strip().lower()
    external_id = str(raw.get("external_id") or raw.get("id") or "").strip()
    if not external_id:
        headline = str(event.headline or "").strip()
        external_id = f"{event.published_at.isoformat()}:{headline[:240]}"
    updated_at = _optional_timestamp(raw.get("updated_at"))
    return {
        "external_id": external_id,
        "symbol": event.symbol,
        "event_type": event.event_type,
        "published_at": event.published_at.astimezone(timezone.utc),
        "updated_at": updated_at,
        "direction": event.direction,
        "magnitude": event.magnitude,
        "source_quality": event.source_quality,
        "novelty": event.novelty,
        "volatility_risk": event.volatility_risk,
        "headline": event.headline,
        "source": source,
        "url": str(raw.get("url") or "").strip() or None,
        "metadata_json": json.dumps(dict(raw.get("metadata") or {}), separators=(",", ":"), default=str),
    }


def _row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "external_id": row[0],
        "symbol": row[1],
        "event_type": row[2],
        "published_at": _iso(row[3]),
        "updated_at": _iso(row[4]) if row[4] is not None else None,
        "direction": row[5],
        "magnitude": float(row[6]),
        "source_quality": float(row[7]),
        "novelty": float(row[8]),
        "volatility_risk": float(row[9]),
        "headline": row[10],
        "source": row[11],
        "url": row[12],
        "metadata": json.loads(str(row[13])) if row[13] else {},
        "ingested_at": _iso(row[14]),
    }


def _timestamp(value: object, name: str) -> datetime:
    parsed = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(parsed):
        raise ValueError(f"{name} must be a valid timestamp")
    return pd.Timestamp(parsed).to_pydatetime().astimezone(timezone.utc)


def _optional_timestamp(value: object) -> datetime | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return _timestamp(value, "timestamp")
    except ValueError:
        return None


def _symbols(values: Iterable[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        symbol = str(value or "").strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            output.append(symbol)
    return output


def _iso(value: object) -> str:
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    return str(value)


__all__ = ["CatalystEventStore"]
