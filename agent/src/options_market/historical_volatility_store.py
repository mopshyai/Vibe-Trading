"""Provider-neutral point-in-time historical option IV/Greeks store.

Historical volatility observations are separate from exchange quote/reference
rows. This preserves provenance and makes it impossible for a later calculated
IV/Greek observation to silently rewrite an earlier OPRA quote.
"""

from __future__ import annotations

from pathlib import Path
import re
import threading
from typing import Iterable

import duckdb
import numpy as np
import pandas as pd

_SCHEMA_VERSION = "1"
_OCC_RE = re.compile(r"^([A-Z0-9]{1,6})(\d{6})([CP])(\d{8})$")
_REQUIRED = {
    "contract_symbol",
    "event_ts",
    "implied_volatility",
    "source",
    "available_at",
}
_OPTIONAL_NUMERIC = (
    "delta",
    "gamma",
    "theta",
    "vega",
    "rho",
    "underlying_price",
    "risk_free_rate",
    "dividend_yield",
)


class HistoricalOptionVolatilityStore:
    """Append-only DuckDB store for point-in-time IV/Greek observations."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._connection = duckdb.connect(self.path)
        self._initialize()

    def __enter__(self) -> "HistoricalOptionVolatilityStore":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:  # type: ignore[no-untyped-def]
        del exc_type, exc, traceback
        self.close()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def ingest(self, observations: pd.DataFrame) -> int:
        frame = _normalize(observations)
        if frame.empty:
            return 0
        with self._lock:
            self._connection.register("_incoming_option_volatility", frame)
            try:
                self._connection.execute(
                    """
                    INSERT INTO option_volatility_observations (
                        contract_symbol, underlying, event_ts, implied_volatility,
                        delta, gamma, theta, vega, rho, underlying_price,
                        risk_free_rate, dividend_yield, model, vendor_observation_id,
                        source, available_at, ingested_at
                    )
                    SELECT
                        contract_symbol, underlying, event_ts, implied_volatility,
                        delta, gamma, theta, vega, rho, underlying_price,
                        risk_free_rate, dividend_yield, model, vendor_observation_id,
                        source, available_at, CURRENT_TIMESTAMP
                    FROM _incoming_option_volatility
                    """
                )
            finally:
                self._connection.unregister("_incoming_option_volatility")
        return int(len(frame))

    def observations_asof(
        self,
        *,
        as_of: object,
        start: object | None = None,
        end: object | None = None,
        contracts: Iterable[str] | None = None,
        underlyings: Iterable[str] | None = None,
    ) -> pd.DataFrame:
        """Return observation revisions that were knowable at ``as_of``."""
        as_of_ts = _utc_naive(as_of, "as_of")
        end_ts = _utc_naive(end, "end") if end is not None else as_of_ts
        start_ts = _utc_naive(start, "start") if start is not None else None
        if start_ts is not None and end_ts < start_ts:
            raise ValueError("end must be >= start")

        clauses = ["available_at <= ?", "event_ts <= ?"]
        params: list[object] = [as_of_ts, end_ts]
        if start_ts is not None:
            clauses.append("event_ts >= ?")
            params.append(start_ts)
        _append_filter(clauses, params, "contract_symbol", contracts)
        _append_filter(clauses, params, "underlying", underlyings)
        where = " AND ".join(clauses)
        sql = f"""
            SELECT
                contract_symbol, underlying, event_ts, implied_volatility,
                delta, gamma, theta, vega, rho, underlying_price,
                risk_free_rate, dividend_yield, model, vendor_observation_id,
                source, available_at, ingested_at
            FROM (
                SELECT *,
                    ROW_NUMBER() OVER (
                        PARTITION BY contract_symbol, event_ts
                        ORDER BY available_at DESC, ingested_at DESC
                    ) AS revision_rank
                FROM option_volatility_observations
                WHERE {where}
            ) revisions
            WHERE revision_rank = 1
            ORDER BY contract_symbol, event_ts
        """
        with self._lock:
            return self._connection.execute(sql, params).fetchdf()

    def latest_asof(
        self,
        *,
        as_of: object,
        contracts: Iterable[str] | None = None,
        underlyings: Iterable[str] | None = None,
    ) -> pd.DataFrame:
        """Return the latest knowable observation per contract."""
        rows = self.observations_asof(
            as_of=as_of,
            contracts=contracts,
            underlyings=underlyings,
        )
        if rows.empty:
            return rows
        rows = rows.sort_values(["contract_symbol", "event_ts", "available_at"])
        return rows.groupby("contract_symbol", sort=False, as_index=False).tail(1).reset_index(drop=True)

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = int(
                self._connection.execute("SELECT COUNT(*) FROM option_volatility_observations").fetchone()[0]
            )
            contracts = int(
                self._connection.execute(
                    "SELECT COUNT(DISTINCT contract_symbol) FROM option_volatility_observations"
                ).fetchone()[0]
            )
        return {"observations": rows, "contracts": contracts}

    def _initialize(self) -> None:
        with self._lock:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS volatility_store_meta (
                    key VARCHAR PRIMARY KEY,
                    value VARCHAR NOT NULL
                )
                """
            )
            existing = self._connection.execute(
                "SELECT value FROM volatility_store_meta WHERE key = 'schema_version'"
            ).fetchone()
            if existing is None:
                self._connection.execute(
                    "INSERT INTO volatility_store_meta VALUES ('schema_version', ?)",
                    [_SCHEMA_VERSION],
                )
            elif str(existing[0]) != _SCHEMA_VERSION:
                raise RuntimeError(
                    f"unsupported historical-volatility store schema {existing[0]!r}; expected {_SCHEMA_VERSION}"
                )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS option_volatility_observations (
                    contract_symbol VARCHAR NOT NULL,
                    underlying VARCHAR NOT NULL,
                    event_ts TIMESTAMP NOT NULL,
                    implied_volatility DOUBLE NOT NULL,
                    delta DOUBLE,
                    gamma DOUBLE,
                    theta DOUBLE,
                    vega DOUBLE,
                    rho DOUBLE,
                    underlying_price DOUBLE,
                    risk_free_rate DOUBLE,
                    dividend_yield DOUBLE,
                    model VARCHAR,
                    vendor_observation_id VARCHAR,
                    source VARCHAR NOT NULL,
                    available_at TIMESTAMP NOT NULL,
                    ingested_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_option_vol_contract_time "
                "ON option_volatility_observations(contract_symbol, event_ts, available_at)"
            )


def _normalize(frame: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise TypeError("historical volatility observations must be a pandas DataFrame")
    missing = _REQUIRED.difference(frame.columns)
    if missing:
        raise ValueError(f"historical volatility observations missing columns: {sorted(missing)}")
    if frame.empty:
        return frame.copy()

    output = frame.copy()
    output["contract_symbol"] = output["contract_symbol"].map(_contract)
    if "underlying" not in output.columns:
        output["underlying"] = output["contract_symbol"].map(_underlying_from_contract)
    else:
        output["underlying"] = output["underlying"].map(_underlying)
    output["event_ts"] = _timestamp_series(output["event_ts"], "event_ts")
    output["available_at"] = _timestamp_series(output["available_at"], "available_at")
    if (output["available_at"] < output["event_ts"]).any():
        raise ValueError("historical volatility available_at cannot precede event_ts")

    output["implied_volatility"] = _numeric(output["implied_volatility"], "implied_volatility")
    if (output["implied_volatility"] <= 0).any() or (output["implied_volatility"] > 10.0).any():
        raise ValueError("implied_volatility must be stored as a positive fraction <= 10")

    for column in _OPTIONAL_NUMERIC:
        if column not in output.columns:
            output[column] = np.nan
        output[column] = pd.to_numeric(output[column], errors="coerce")
        finite = output[column].dropna()
        if not finite.empty and not np.isfinite(finite.to_numpy(dtype=float)).all():
            raise ValueError(f"{column} contains non-finite values")

    if output["delta"].dropna().abs().gt(1.0).any():
        raise ValueError("delta must be between -1 and 1 when present")
    if output["gamma"].dropna().lt(0).any():
        raise ValueError("gamma cannot be negative when present")
    if output["vega"].dropna().lt(0).any():
        raise ValueError("vega cannot be negative when present")
    if output["underlying_price"].dropna().le(0).any():
        raise ValueError("underlying_price must be positive when present")
    if output["dividend_yield"].dropna().lt(0).any():
        raise ValueError("dividend_yield cannot be negative when present")

    output["source"] = output["source"].astype(str).str.strip()
    if (output["source"] == "").any():
        raise ValueError("source must be non-empty")
    for column in ("model", "vendor_observation_id"):
        if column not in output.columns:
            output[column] = None
        output[column] = output[column].where(output[column].notna(), None)

    columns = [
        "contract_symbol",
        "underlying",
        "event_ts",
        "implied_volatility",
        "delta",
        "gamma",
        "theta",
        "vega",
        "rho",
        "underlying_price",
        "risk_free_rate",
        "dividend_yield",
        "model",
        "vendor_observation_id",
        "source",
        "available_at",
    ]
    return output[columns]


def _contract(value: object) -> str:
    contract = str(value or "").strip().upper().replace(" ", "")
    if not _OCC_RE.fullmatch(contract):
        raise ValueError(f"invalid OCC/OSI contract symbol: {value!r}")
    return contract


def _underlying(value: object) -> str:
    symbol = str(value or "").strip().upper()
    if not symbol:
        raise ValueError("underlying cannot be empty")
    return symbol


def _underlying_from_contract(contract: str) -> str:
    match = _OCC_RE.fullmatch(contract)
    assert match is not None
    return f"{match.group(1)}.US"


def _timestamp_series(series: pd.Series, name: str) -> pd.Series:
    parsed = pd.to_datetime(series, utc=True, errors="coerce")
    if parsed.isna().any():
        raise ValueError(f"{name} contains invalid timestamps")
    return parsed.dt.tz_convert(None)


def _utc_naive(value: object, name: str) -> pd.Timestamp:
    parsed = pd.Timestamp(value)
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return parsed.tz_convert("UTC").tz_localize(None)


def _numeric(series: pd.Series, name: str) -> pd.Series:
    parsed = pd.to_numeric(series, errors="coerce")
    if parsed.isna().any() or not np.isfinite(parsed.to_numpy(dtype=float)).all():
        raise ValueError(f"{name} contains non-finite values")
    return parsed.astype(float)


def _append_filter(
    clauses: list[str],
    params: list[object],
    column: str,
    values: Iterable[str] | None,
) -> None:
    normalized = []
    seen: set[str] = set()
    for value in values or []:
        token = str(value or "").strip().upper().replace(" ", "") if column == "contract_symbol" else str(value or "").strip().upper()
        if token and token not in seen:
            seen.add(token)
            normalized.append(token)
    if normalized:
        placeholders = ",".join("?" for _ in normalized)
        clauses.append(f"{column} IN ({placeholders})")
        params.extend(normalized)


__all__ = ["HistoricalOptionVolatilityStore"]
