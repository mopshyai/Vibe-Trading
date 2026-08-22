"""Point-in-time DuckDB store for U.S. equity and option research data.

A market timestamp answers "when did this observation occur?" while
``available_at`` answers "when could the research system have known it?". Keeping
both is the core anti-lookahead invariant. Revisions are preserved rather than
overwritten; as-of queries select the latest revision that was available at the
requested research time.

This module stores data only. It has no network or broker dependency.
"""

from __future__ import annotations

from pathlib import Path
import threading
from typing import Iterable

import duckdb
import numpy as np
import pandas as pd

_SCHEMA_VERSION = "1"

_EQUITY_REQUIRED = {
    "symbol",
    "event_ts",
    "interval",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "source",
    "available_at",
}

_OPTION_REQUIRED = {
    "contract_symbol",
    "underlying",
    "event_ts",
    "expiration",
    "strike",
    "option_type",
    "bid",
    "ask",
    "source",
    "available_at",
}


class OptionsResearchStore:
    """Persistent point-in-time research store backed by DuckDB."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._connection = duckdb.connect(self.path)
        self._initialize_schema()

    def __enter__(self) -> "OptionsResearchStore":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:  # type: ignore[no-untyped-def]
        del exc_type, exc, traceback
        self.close()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def ingest_equity_bars(self, bars: pd.DataFrame) -> int:
        """Append validated equity bars while preserving later revisions."""
        frame = _normalize_equity_bars(bars)
        if frame.empty:
            return 0
        with self._lock:
            self._connection.register("_incoming_equity_bars", frame)
            try:
                self._connection.execute(
                    """
                    INSERT INTO equity_bars (
                        symbol, event_ts, interval, open, high, low, close, volume,
                        source, available_at, ingested_at
                    )
                    SELECT
                        symbol, event_ts, interval, open, high, low, close, volume,
                        source, available_at, CURRENT_TIMESTAMP
                    FROM _incoming_equity_bars
                    """
                )
            finally:
                self._connection.unregister("_incoming_equity_bars")
        return int(len(frame))

    def ingest_option_quotes(self, quotes: pd.DataFrame) -> int:
        """Append validated option quotes while preserving later revisions."""
        frame = _normalize_option_quotes(quotes)
        if frame.empty:
            return 0
        with self._lock:
            self._connection.register("_incoming_option_quotes", frame)
            try:
                self._connection.execute(
                    """
                    INSERT INTO option_quotes (
                        contract_symbol, underlying, event_ts, expiration, strike,
                        option_type, bid, ask, last, volume, open_interest,
                        implied_volatility, source, available_at, ingested_at
                    )
                    SELECT
                        contract_symbol, underlying, event_ts, expiration, strike,
                        option_type, bid, ask, last, volume, open_interest,
                        implied_volatility, source, available_at, CURRENT_TIMESTAMP
                    FROM _incoming_option_quotes
                    """
                )
            finally:
                self._connection.unregister("_incoming_option_quotes")
        return int(len(frame))

    def equity_bars_asof(
        self,
        symbols: Iterable[str],
        *,
        start: object,
        end: object,
        as_of: object,
        interval: str = "1D",
    ) -> pd.DataFrame:
        """Return the latest equity revision knowable at ``as_of``."""
        normalized_symbols = _symbols(symbols)
        if not normalized_symbols:
            return _empty_equity_result()
        start_ts = _utc_naive_timestamp(start, "start")
        end_ts = _utc_naive_timestamp(end, "end")
        as_of_ts = _utc_naive_timestamp(as_of, "as_of")
        if end_ts < start_ts:
            raise ValueError("end must be >= start")

        placeholders = ",".join("?" for _ in normalized_symbols)
        sql = f"""
            SELECT
                symbol, event_ts, interval, open, high, low, close, volume,
                source, available_at, ingested_at
            FROM (
                SELECT *,
                    ROW_NUMBER() OVER (
                        PARTITION BY symbol, event_ts, interval
                        ORDER BY available_at DESC, ingested_at DESC
                    ) AS revision_rank
                FROM equity_bars
                WHERE symbol IN ({placeholders})
                  AND interval = ?
                  AND event_ts >= ?
                  AND event_ts <= ?
                  AND available_at <= ?
            ) revisions
            WHERE revision_rank = 1
            ORDER BY symbol, event_ts
        """
        params = [*normalized_symbols, str(interval), start_ts, end_ts, as_of_ts]
        with self._lock:
            return self._connection.execute(sql, params).fetchdf()

    def equity_frames_asof(
        self,
        symbols: Iterable[str],
        *,
        start: object,
        end: object,
        as_of: object,
        interval: str = "1D",
    ) -> dict[str, pd.DataFrame]:
        """Return point-in-time OHLCV frames suitable for the chart scanner."""
        rows = self.equity_bars_asof(
            symbols,
            start=start,
            end=end,
            as_of=as_of,
            interval=interval,
        )
        output: dict[str, pd.DataFrame] = {}
        if rows.empty:
            return output
        for symbol, group in rows.groupby("symbol", sort=False):
            frame = group.set_index("event_ts")[["open", "high", "low", "close", "volume"]].copy()
            frame.index = pd.DatetimeIndex(frame.index)
            output[str(symbol)] = frame.sort_index()
        return output

    def option_quotes_asof(
        self,
        *,
        as_of: object,
        start: object | None = None,
        end: object | None = None,
        underlyings: Iterable[str] | None = None,
        contracts: Iterable[str] | None = None,
    ) -> pd.DataFrame:
        """Return latest knowable option-quote revisions for a research window."""
        as_of_ts = _utc_naive_timestamp(as_of, "as_of")
        start_ts = _utc_naive_timestamp(start, "start") if start is not None else None
        end_ts = _utc_naive_timestamp(end, "end") if end is not None else as_of_ts
        if start_ts is not None and end_ts < start_ts:
            raise ValueError("end must be >= start")

        clauses = ["available_at <= ?", "event_ts <= ?"]
        params: list[object] = [as_of_ts, end_ts]
        if start_ts is not None:
            clauses.append("event_ts >= ?")
            params.append(start_ts)

        normalized_underlyings = _symbols(underlyings or [])
        if normalized_underlyings:
            placeholders = ",".join("?" for _ in normalized_underlyings)
            clauses.append(f"underlying IN ({placeholders})")
            params.extend(normalized_underlyings)

        normalized_contracts = _symbols(contracts or [])
        if normalized_contracts:
            placeholders = ",".join("?" for _ in normalized_contracts)
            clauses.append(f"contract_symbol IN ({placeholders})")
            params.extend(normalized_contracts)

        where = " AND ".join(clauses)
        sql = f"""
            SELECT
                contract_symbol, underlying, event_ts, expiration, strike,
                option_type, bid, ask, last, volume, open_interest,
                implied_volatility, source, available_at, ingested_at
            FROM (
                SELECT *,
                    ROW_NUMBER() OVER (
                        PARTITION BY contract_symbol, event_ts
                        ORDER BY available_at DESC, ingested_at DESC
                    ) AS revision_rank
                FROM option_quotes
                WHERE {where}
            ) revisions
            WHERE revision_rank = 1
            ORDER BY underlying, contract_symbol, event_ts
        """
        with self._lock:
            return self._connection.execute(sql, params).fetchdf()

    def counts(self) -> dict[str, int]:
        """Return raw stored row counts, including preserved revisions."""
        with self._lock:
            equity = int(self._connection.execute("SELECT COUNT(*) FROM equity_bars").fetchone()[0])
            options = int(self._connection.execute("SELECT COUNT(*) FROM option_quotes").fetchone()[0])
        return {"equity_bars": equity, "option_quotes": options}

    def _initialize_schema(self) -> None:
        with self._lock:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS research_store_meta (
                    key VARCHAR PRIMARY KEY,
                    value VARCHAR NOT NULL
                )
                """
            )
            existing = self._connection.execute(
                "SELECT value FROM research_store_meta WHERE key = 'schema_version'"
            ).fetchone()
            if existing is None:
                self._connection.execute(
                    "INSERT INTO research_store_meta VALUES ('schema_version', ?)",
                    [_SCHEMA_VERSION],
                )
            elif str(existing[0]) != _SCHEMA_VERSION:
                raise RuntimeError(
                    f"unsupported options research-store schema {existing[0]!r}; expected {_SCHEMA_VERSION}"
                )

            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS equity_bars (
                    symbol VARCHAR NOT NULL,
                    event_ts TIMESTAMP NOT NULL,
                    interval VARCHAR NOT NULL,
                    open DOUBLE NOT NULL,
                    high DOUBLE NOT NULL,
                    low DOUBLE NOT NULL,
                    close DOUBLE NOT NULL,
                    volume DOUBLE NOT NULL,
                    source VARCHAR NOT NULL,
                    available_at TIMESTAMP NOT NULL,
                    ingested_at TIMESTAMP NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS option_quotes (
                    contract_symbol VARCHAR NOT NULL,
                    underlying VARCHAR NOT NULL,
                    event_ts TIMESTAMP NOT NULL,
                    expiration TIMESTAMP NOT NULL,
                    strike DOUBLE NOT NULL,
                    option_type VARCHAR NOT NULL,
                    bid DOUBLE NOT NULL,
                    ask DOUBLE NOT NULL,
                    last DOUBLE,
                    volume BIGINT,
                    open_interest BIGINT,
                    implied_volatility DOUBLE,
                    source VARCHAR NOT NULL,
                    available_at TIMESTAMP NOT NULL,
                    ingested_at TIMESTAMP NOT NULL
                )
                """
            )


def _normalize_equity_bars(bars: pd.DataFrame) -> pd.DataFrame:
    frame = _require_frame(bars, _EQUITY_REQUIRED, "equity bars")
    if frame.empty:
        return frame
    frame = frame.copy()
    frame["symbol"] = frame["symbol"].map(_symbol)
    frame["interval"] = frame["interval"].astype(str).str.strip()
    frame["source"] = frame["source"].astype(str).str.strip()
    frame["event_ts"] = _timestamp_series(frame["event_ts"], "event_ts")
    frame["available_at"] = _timestamp_series(frame["available_at"], "available_at")
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = _numeric_series(frame[column], column)

    if (frame[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("equity OHLC prices must be positive")
    if (frame["volume"] < 0).any():
        raise ValueError("equity volume must be non-negative")
    if (frame["available_at"] < frame["event_ts"]).any():
        raise ValueError("available_at cannot precede event_ts")
    if (frame["interval"] == "").any() or (frame["source"] == "").any():
        raise ValueError("interval and source must be non-empty")
    if (frame["high"] < frame[["open", "low", "close"]].max(axis=1)).any():
        raise ValueError("equity high must be >= open, low, and close")
    if (frame["low"] > frame[["open", "high", "close"]].min(axis=1)).any():
        raise ValueError("equity low must be <= open, high, and close")
    return frame[list(_EQUITY_REQUIRED)]


def _normalize_option_quotes(quotes: pd.DataFrame) -> pd.DataFrame:
    frame = _require_frame(quotes, _OPTION_REQUIRED, "option quotes")
    if frame.empty:
        return frame
    frame = frame.copy()
    frame["contract_symbol"] = frame["contract_symbol"].map(_symbol)
    frame["underlying"] = frame["underlying"].map(_symbol)
    frame["option_type"] = frame["option_type"].astype(str).str.strip().str.lower()
    frame["source"] = frame["source"].astype(str).str.strip()
    frame["event_ts"] = _timestamp_series(frame["event_ts"], "event_ts")
    frame["available_at"] = _timestamp_series(frame["available_at"], "available_at")
    frame["expiration"] = _timestamp_series(frame["expiration"], "expiration")

    for column in ("strike", "bid", "ask"):
        frame[column] = _numeric_series(frame[column], column)
    for optional in ("last", "volume", "open_interest", "implied_volatility"):
        if optional not in frame.columns:
            frame[optional] = np.nan
        frame[optional] = pd.to_numeric(frame[optional], errors="coerce")
        if np.isinf(frame[optional].fillna(0.0)).any():
            raise ValueError(f"{optional} must be finite when present")

    if (frame["strike"] <= 0).any():
        raise ValueError("option strike must be positive")
    if (frame[["bid", "ask"]] < 0).any().any():
        raise ValueError("option bid/ask must be non-negative")
    if (frame["ask"] < frame["bid"]).any():
        raise ValueError("option ask cannot be below bid")
    if (~frame["option_type"].isin(["call", "put"])).any():
        raise ValueError("option_type must be 'call' or 'put'")
    if (frame["available_at"] < frame["event_ts"]).any():
        raise ValueError("available_at cannot precede event_ts")
    if (frame["expiration"] <= frame["event_ts"]).any():
        raise ValueError("option expiration must be after event_ts")
    if (frame["source"] == "").any():
        raise ValueError("source must be non-empty")
    for nonnegative in ("last", "volume", "open_interest", "implied_volatility"):
        values = frame[nonnegative].dropna()
        if (values < 0).any():
            raise ValueError(f"{nonnegative} must be non-negative when present")

    ordered = [
        "contract_symbol",
        "underlying",
        "event_ts",
        "expiration",
        "strike",
        "option_type",
        "bid",
        "ask",
        "last",
        "volume",
        "open_interest",
        "implied_volatility",
        "source",
        "available_at",
    ]
    return frame[ordered]


def _require_frame(frame: pd.DataFrame, required: set[str], label: str) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise TypeError(f"{label} must be a pandas DataFrame")
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(f"{label} missing required columns: {missing}")
    return frame


def _timestamp_series(series: pd.Series, name: str) -> pd.Series:
    parsed = pd.to_datetime(series, errors="coerce", utc=True)
    if parsed.isna().any():
        raise ValueError(f"{name} contains invalid timestamps")
    return parsed.dt.tz_convert(None)


def _utc_naive_timestamp(value: object, name: str) -> pd.Timestamp:
    parsed = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(parsed):
        raise ValueError(f"{name} must be a valid timestamp")
    return pd.Timestamp(parsed).tz_convert(None)


def _numeric_series(series: pd.Series, name: str) -> pd.Series:
    parsed = pd.to_numeric(series, errors="coerce")
    if parsed.isna().any() or np.isinf(parsed).any():
        raise ValueError(f"{name} must contain only finite numbers")
    return parsed.astype(float)


def _symbol(value: object) -> str:
    symbol = str(value or "").strip().upper()
    if not symbol or any(character.isspace() for character in symbol):
        raise ValueError("symbols must be non-empty and contain no whitespace")
    return symbol


def _symbols(values: Iterable[str]) -> list[str]:
    return sorted({_symbol(value) for value in values})


def _empty_equity_result() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "symbol",
            "event_ts",
            "interval",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "source",
            "available_at",
            "ingested_at",
        ]
    )


__all__ = ["OptionsResearchStore"]
