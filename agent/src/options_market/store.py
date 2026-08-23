"""Point-in-time DuckDB store for U.S. equity and option research data.

A market timestamp answers "when did this observation occur?" while
``available_at`` answers "when could the research system have known it?". Keeping
both is the core anti-lookahead invariant. Revisions are preserved rather than
overwritten; as-of queries select the latest revision that was available at the
requested research time.

Option definitions and published statistics live in separate additive tables so
open interest or daily volume cannot be backfilled onto an earlier quote before
those values were actually knowable.

This module stores data only. It has no network or broker dependency.
"""

from __future__ import annotations

from pathlib import Path
import threading
from typing import Iterable

import duckdb
import numpy as np
import pandas as pd

_SCHEMA_VERSION = "2"
_SUPPORTED_SCHEMA_VERSIONS = {"1", "2"}

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

_OPTION_DEFINITION_REQUIRED = {
    "contract_symbol",
    "underlying",
    "event_ts",
    "expiration",
    "strike",
    "option_type",
    "source",
    "available_at",
}

_OPTION_STAT_REQUIRED = {
    "contract_symbol",
    "underlying",
    "event_ts",
    "metric",
    "value",
    "source",
    "available_at",
}

_OPTION_STAT_METRICS = {"open_interest", "daily_volume"}


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

    def ingest_option_definitions(self, definitions: pd.DataFrame) -> int:
        """Append point-in-time option reference definitions."""
        frame = _normalize_option_definitions(definitions)
        if frame.empty:
            return 0
        with self._lock:
            self._connection.register("_incoming_option_definitions", frame)
            try:
                self._connection.execute(
                    """
                    INSERT INTO option_definitions (
                        contract_symbol, underlying, event_ts, expiration, strike,
                        option_type, activation, min_price_increment,
                        contract_multiplier, update_action, raw_symbol,
                        source, available_at, ingested_at
                    )
                    SELECT
                        contract_symbol, underlying, event_ts, expiration, strike,
                        option_type, activation, min_price_increment,
                        contract_multiplier, update_action, raw_symbol,
                        source, available_at, CURRENT_TIMESTAMP
                    FROM _incoming_option_definitions
                    """
                )
            finally:
                self._connection.unregister("_incoming_option_definitions")
        return int(len(frame))

    def ingest_option_statistics(self, statistics: pd.DataFrame) -> int:
        """Append point-in-time option statistics such as OI and daily volume."""
        frame = _normalize_option_statistics(statistics)
        if frame.empty:
            return 0
        with self._lock:
            self._connection.register("_incoming_option_statistics", frame)
            try:
                self._connection.execute(
                    """
                    INSERT INTO option_statistics (
                        contract_symbol, underlying, event_ts, reference_ts,
                        metric, value, source, available_at, ingested_at
                    )
                    SELECT
                        contract_symbol, underlying, event_ts, reference_ts,
                        metric, value, source, available_at, CURRENT_TIMESTAMP
                    FROM _incoming_option_statistics
                    """
                )
            finally:
                self._connection.unregister("_incoming_option_statistics")
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

    def option_definitions_asof(
        self,
        *,
        as_of: object,
        underlyings: Iterable[str] | None = None,
        contracts: Iterable[str] | None = None,
    ) -> pd.DataFrame:
        """Return latest option definitions that were available at ``as_of``."""
        as_of_ts = _utc_naive_timestamp(as_of, "as_of")
        clauses = ["available_at <= ?", "event_ts <= ?"]
        params: list[object] = [as_of_ts, as_of_ts]
        _append_symbol_filters(clauses, params, underlyings=underlyings, contracts=contracts)
        where = " AND ".join(clauses)
        sql = f"""
            SELECT
                contract_symbol, underlying, event_ts, expiration, strike,
                option_type, activation, min_price_increment,
                contract_multiplier, update_action, raw_symbol,
                source, available_at, ingested_at
            FROM (
                SELECT *,
                    ROW_NUMBER() OVER (
                        PARTITION BY contract_symbol
                        ORDER BY available_at DESC, event_ts DESC, ingested_at DESC
                    ) AS revision_rank
                FROM option_definitions
                WHERE {where}
            ) revisions
            WHERE revision_rank = 1
            ORDER BY underlying, contract_symbol
        """
        with self._lock:
            return self._connection.execute(sql, params).fetchdf()

    def option_statistics_asof(
        self,
        *,
        as_of: object,
        underlyings: Iterable[str] | None = None,
        contracts: Iterable[str] | None = None,
        metrics: Iterable[str] | None = None,
    ) -> pd.DataFrame:
        """Return latest knowable value per contract/statistic at ``as_of``."""
        as_of_ts = _utc_naive_timestamp(as_of, "as_of")
        clauses = ["available_at <= ?", "event_ts <= ?"]
        params: list[object] = [as_of_ts, as_of_ts]
        _append_symbol_filters(clauses, params, underlyings=underlyings, contracts=contracts)
        metric_values = _metrics(metrics or [])
        if metric_values:
            placeholders = ",".join("?" for _ in metric_values)
            clauses.append(f"metric IN ({placeholders})")
            params.extend(metric_values)
        where = " AND ".join(clauses)
        sql = f"""
            SELECT
                contract_symbol, underlying, event_ts, reference_ts,
                metric, value, source, available_at, ingested_at
            FROM (
                SELECT *,
                    ROW_NUMBER() OVER (
                        PARTITION BY contract_symbol, metric
                        ORDER BY available_at DESC, event_ts DESC, ingested_at DESC
                    ) AS revision_rank
                FROM option_statistics
                WHERE {where}
            ) revisions
            WHERE revision_rank = 1
            ORDER BY underlying, contract_symbol, metric
        """
        with self._lock:
            return self._connection.execute(sql, params).fetchdf()

    def option_quotes_with_metadata_asof(
        self,
        *,
        as_of: object,
        start: object | None = None,
        end: object | None = None,
        underlyings: Iterable[str] | None = None,
        contracts: Iterable[str] | None = None,
    ) -> pd.DataFrame:
        """Return quote revisions enriched only with metadata knowable at ``as_of``.

        Quote-native volume/open-interest values win when present. Otherwise the
        latest published point-in-time statistic is used. Daily volume therefore
        becomes visible only after that daily bar's ``available_at`` timestamp.
        """
        quotes = self.option_quotes_asof(
            as_of=as_of,
            start=start,
            end=end,
            underlyings=underlyings,
            contracts=contracts,
        )
        if quotes.empty:
            return quotes
        contract_values = [str(value) for value in quotes["contract_symbol"].dropna().unique()]
        definitions = self.option_definitions_asof(as_of=as_of, contracts=contract_values)
        statistics = self.option_statistics_asof(
            as_of=as_of,
            contracts=contract_values,
            metrics=["open_interest", "daily_volume"],
        )
        enriched = quotes.copy()

        if not definitions.empty:
            def_rows = definitions[
                [
                    "contract_symbol",
                    "activation",
                    "min_price_increment",
                    "contract_multiplier",
                    "update_action",
                    "raw_symbol",
                    "available_at",
                ]
            ].rename(columns={"available_at": "definition_available_at"})
            enriched = enriched.merge(def_rows, on="contract_symbol", how="left")
        else:
            for column in (
                "activation",
                "min_price_increment",
                "contract_multiplier",
                "update_action",
                "raw_symbol",
                "definition_available_at",
            ):
                enriched[column] = None

        if not statistics.empty:
            values = statistics.pivot(index="contract_symbol", columns="metric", values="value").reset_index()
            availability = statistics.pivot(
                index="contract_symbol", columns="metric", values="available_at"
            ).reset_index()
            availability = availability.rename(
                columns={
                    "open_interest": "open_interest_available_at",
                    "daily_volume": "daily_volume_available_at",
                }
            )
            values = values.rename(columns={"daily_volume": "published_daily_volume"})
            enriched = enriched.merge(values, on="contract_symbol", how="left")
            enriched = enriched.merge(availability, on="contract_symbol", how="left")
            if "open_interest_y" in enriched.columns:
                quote_oi = pd.to_numeric(enriched.get("open_interest_x"), errors="coerce")
                stat_oi = pd.to_numeric(enriched.get("open_interest_y"), errors="coerce")
                enriched["open_interest"] = quote_oi.where(quote_oi.notna(), stat_oi)
                enriched = enriched.drop(columns=["open_interest_x", "open_interest_y"])
            if "published_daily_volume" in enriched.columns:
                quote_volume = pd.to_numeric(enriched.get("volume"), errors="coerce")
                daily_volume = pd.to_numeric(enriched.get("published_daily_volume"), errors="coerce")
                enriched["volume"] = quote_volume.where(quote_volume.notna(), daily_volume)
        else:
            enriched["published_daily_volume"] = None
            enriched["open_interest_available_at"] = None
            enriched["daily_volume_available_at"] = None
        return enriched

    def counts(self) -> dict[str, int]:
        """Return raw stored row counts, including preserved revisions."""
        with self._lock:
            equity = int(self._connection.execute("SELECT COUNT(*) FROM equity_bars").fetchone()[0])
            options = int(self._connection.execute("SELECT COUNT(*) FROM option_quotes").fetchone()[0])
            definitions = int(self._connection.execute("SELECT COUNT(*) FROM option_definitions").fetchone()[0])
            statistics = int(self._connection.execute("SELECT COUNT(*) FROM option_statistics").fetchone()[0])
        return {
            "equity_bars": equity,
            "option_quotes": options,
            "option_definitions": definitions,
            "option_statistics": statistics,
        }

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
            existing_version = None if existing is None else str(existing[0])
            if existing_version is not None and existing_version not in _SUPPORTED_SCHEMA_VERSIONS:
                raise RuntimeError(
                    f"unsupported options research-store schema {existing_version!r}; expected one of {sorted(_SUPPORTED_SCHEMA_VERSIONS)}"
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
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS option_definitions (
                    contract_symbol VARCHAR NOT NULL,
                    underlying VARCHAR NOT NULL,
                    event_ts TIMESTAMP NOT NULL,
                    expiration TIMESTAMP NOT NULL,
                    strike DOUBLE NOT NULL,
                    option_type VARCHAR NOT NULL,
                    activation TIMESTAMP,
                    min_price_increment DOUBLE,
                    contract_multiplier DOUBLE,
                    update_action VARCHAR,
                    raw_symbol VARCHAR,
                    source VARCHAR NOT NULL,
                    available_at TIMESTAMP NOT NULL,
                    ingested_at TIMESTAMP NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS option_statistics (
                    contract_symbol VARCHAR NOT NULL,
                    underlying VARCHAR NOT NULL,
                    event_ts TIMESTAMP NOT NULL,
                    reference_ts TIMESTAMP,
                    metric VARCHAR NOT NULL,
                    value DOUBLE NOT NULL,
                    source VARCHAR NOT NULL,
                    available_at TIMESTAMP NOT NULL,
                    ingested_at TIMESTAMP NOT NULL
                )
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_option_def_contract ON option_definitions(contract_symbol, available_at)"
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_option_stats_contract ON option_statistics(contract_symbol, metric, available_at)"
            )

            if existing_version is None:
                self._connection.execute(
                    "INSERT INTO research_store_meta VALUES ('schema_version', ?)",
                    [_SCHEMA_VERSION],
                )
            elif existing_version == "1":
                # Additive v1 -> v2 migration: existing quote/bar data is retained;
                # only the new reference/statistics tables and indexes are added.
                self._connection.execute(
                    "UPDATE research_store_meta SET value = ? WHERE key = 'schema_version'",
                    [_SCHEMA_VERSION],
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

    if (~frame["option_type"].isin({"call", "put"})).any():
        raise ValueError("option_type must be call or put")
    if (frame["strike"] <= 0).any():
        raise ValueError("option strike must be positive")
    if (frame[["bid", "ask"]] < 0).any().any():
        raise ValueError("option bid/ask must be non-negative")
    if (frame["ask"] < frame["bid"]).any():
        raise ValueError("option ask cannot be below bid")
    if (frame["expiration"] <= frame["event_ts"]).any():
        raise ValueError("option expiration must be after event_ts")
    if (frame["available_at"] < frame["event_ts"]).any():
        raise ValueError("available_at cannot precede event_ts")
    if frame["volume"].dropna().lt(0).any() or frame["open_interest"].dropna().lt(0).any():
        raise ValueError("option volume/open_interest cannot be negative")
    if frame["implied_volatility"].dropna().le(0).any():
        raise ValueError("implied_volatility must be positive when present")
    if (frame["source"] == "").any():
        raise ValueError("source must be non-empty")
    columns = [
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
    return frame[columns]


def _normalize_option_definitions(definitions: pd.DataFrame) -> pd.DataFrame:
    frame = _require_frame(definitions, _OPTION_DEFINITION_REQUIRED, "option definitions")
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
    if "activation" not in frame.columns:
        frame["activation"] = pd.NaT
    frame["activation"] = pd.to_datetime(frame["activation"], utc=True, errors="coerce").dt.tz_convert(None)
    for optional in ("min_price_increment", "contract_multiplier"):
        if optional not in frame.columns:
            frame[optional] = np.nan
        frame[optional] = pd.to_numeric(frame[optional], errors="coerce")
    for optional in ("update_action", "raw_symbol"):
        if optional not in frame.columns:
            frame[optional] = None
        frame[optional] = frame[optional].where(frame[optional].notna(), None)

    if (~frame["option_type"].isin({"call", "put"})).any():
        raise ValueError("option definition option_type must be call or put")
    frame["strike"] = _numeric_series(frame["strike"], "strike")
    if (frame["strike"] <= 0).any():
        raise ValueError("option definition strike must be positive")
    if (frame["expiration"] <= frame["event_ts"]).any():
        raise ValueError("option definition expiration must be after event_ts")
    if (frame["available_at"] < frame["event_ts"]).any():
        raise ValueError("option definition available_at cannot precede event_ts")
    if frame["min_price_increment"].dropna().le(0).any():
        raise ValueError("min_price_increment must be positive when present")
    if frame["contract_multiplier"].dropna().le(0).any():
        raise ValueError("contract_multiplier must be positive when present")
    if (frame["source"] == "").any():
        raise ValueError("source must be non-empty")
    columns = [
        "contract_symbol",
        "underlying",
        "event_ts",
        "expiration",
        "strike",
        "option_type",
        "activation",
        "min_price_increment",
        "contract_multiplier",
        "update_action",
        "raw_symbol",
        "source",
        "available_at",
    ]
    return frame[columns]


def _normalize_option_statistics(statistics: pd.DataFrame) -> pd.DataFrame:
    frame = _require_frame(statistics, _OPTION_STAT_REQUIRED, "option statistics")
    if frame.empty:
        return frame
    frame = frame.copy()
    frame["contract_symbol"] = frame["contract_symbol"].map(_symbol)
    frame["underlying"] = frame["underlying"].map(_symbol)
    frame["metric"] = frame["metric"].astype(str).str.strip().str.lower()
    frame["source"] = frame["source"].astype(str).str.strip()
    frame["event_ts"] = _timestamp_series(frame["event_ts"], "event_ts")
    frame["available_at"] = _timestamp_series(frame["available_at"], "available_at")
    if "reference_ts" not in frame.columns:
        frame["reference_ts"] = pd.NaT
    frame["reference_ts"] = pd.to_datetime(frame["reference_ts"], utc=True, errors="coerce").dt.tz_convert(None)
    frame["value"] = _numeric_series(frame["value"], "value")

    if (~frame["metric"].isin(_OPTION_STAT_METRICS)).any():
        raise ValueError(f"option statistic metric must be one of {sorted(_OPTION_STAT_METRICS)}")
    if (frame["value"] < 0).any():
        raise ValueError("option statistic value cannot be negative")
    if (frame["available_at"] < frame["event_ts"]).any():
        raise ValueError("option statistic available_at cannot precede event_ts")
    if (frame["source"] == "").any():
        raise ValueError("source must be non-empty")
    columns = [
        "contract_symbol",
        "underlying",
        "event_ts",
        "reference_ts",
        "metric",
        "value",
        "source",
        "available_at",
    ]
    return frame[columns]


def _append_symbol_filters(
    clauses: list[str],
    params: list[object],
    *,
    underlyings: Iterable[str] | None,
    contracts: Iterable[str] | None,
) -> None:
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


def _metrics(values: Iterable[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        metric = str(value or "").strip().lower()
        if not metric:
            continue
        if metric not in _OPTION_STAT_METRICS:
            raise ValueError(f"unsupported option statistic metric {metric!r}")
        if metric not in seen:
            seen.add(metric)
            output.append(metric)
    return output


def _require_frame(frame: pd.DataFrame, required: set[str], label: str) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise TypeError(f"{label} must be a pandas DataFrame")
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"{label} missing columns: {sorted(missing)}")
    return frame


def _timestamp_series(series: pd.Series, name: str) -> pd.Series:
    parsed = pd.to_datetime(series, utc=True, errors="coerce")
    if parsed.isna().any():
        raise ValueError(f"{name} contains invalid timestamps")
    return parsed.dt.tz_convert(None)


def _utc_naive_timestamp(value: object, name: str) -> pd.Timestamp:
    parsed = pd.Timestamp(value)
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return parsed.tz_convert("UTC").tz_localize(None)


def _numeric_series(series: pd.Series, name: str) -> pd.Series:
    parsed = pd.to_numeric(series, errors="coerce")
    if parsed.isna().any() or not np.isfinite(parsed.to_numpy(dtype=float)).all():
        raise ValueError(f"{name} contains non-finite values")
    return parsed.astype(float)


def _symbol(value: object) -> str:
    symbol = str(value or "").strip().upper()
    if not symbol:
        raise ValueError("symbol cannot be empty")
    return symbol


def _symbols(values: Iterable[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        symbol = _symbol(value)
        if symbol not in seen:
            seen.add(symbol)
            output.append(symbol)
    return output


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
