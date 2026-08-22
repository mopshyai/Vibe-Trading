"""Databento historical-data adapter for the U.S. options research pipeline.

The adapter uses Databento's documented Historical HTTP endpoint directly so the
core package does not require the optional Databento SDK. It streams JSONL into
the point-in-time DuckDB store in bounded chunks.

Supported research paths:
- ``EQUS.SUMMARY`` / ``ohlcv-1d`` for consolidated U.S. equity daily bars.
- ``OPRA.PILLAR`` / ``cbbo-1m`` for consolidated one-minute U.S. option BBO.

No request is made unless a caller explicitly invokes an ingest method and
provides a Databento API key (argument or ``DATABENTO_API_KEY`` environment
variable). The key is never written to disk or included in errors/log payloads.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
import json
import os
from typing import Any, Iterable, Iterator, Mapping
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from .store import OptionsResearchStore

_HISTORICAL_URL = "https://hist.databento.com/v0/timeseries.get_range"
_ET = ZoneInfo("America/New_York")
_UTC = timezone.utc


@dataclass(frozen=True)
class DatabentoHistoricalConfig:
    equity_dataset: str = "EQUS.SUMMARY"
    equity_schema: str = "ohlcv-1d"
    options_dataset: str = "OPRA.PILLAR"
    options_schema: str = "cbbo-1m"
    request_timeout_seconds: float = 180.0
    ingest_chunk_rows: int = 5_000

    def validate(self) -> None:
        if not self.equity_dataset or not self.equity_schema:
            raise ValueError("equity dataset/schema must be non-empty")
        if not self.options_dataset or not self.options_schema:
            raise ValueError("options dataset/schema must be non-empty")
        if self.request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if not 100 <= self.ingest_chunk_rows <= 100_000:
            raise ValueError("ingest_chunk_rows must be between 100 and 100000")


@dataclass(frozen=True)
class OSIContract:
    raw_symbol: str
    root: str
    project_underlying: str
    expiration: datetime
    option_type: str
    strike: float


class DatabentoHistoricalAdapter:
    """Stream Databento historical records into :class:`OptionsResearchStore`."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        config: DatabentoHistoricalConfig | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.config = config or DatabentoHistoricalConfig()
        self.config.validate()
        self._api_key = str(api_key or os.environ.get("DATABENTO_API_KEY") or "").strip()
        if not self._api_key:
            raise RuntimeError(
                "Databento historical access requires an API key argument or DATABENTO_API_KEY"
            )
        self._session = session or requests.Session()

    def ingest_equity_daily_bars(
        self,
        store: OptionsResearchStore,
        *,
        start: object,
        end: object,
        symbols: Iterable[str] | None = None,
    ) -> int:
        """Stream consolidated U.S. daily OHLCV into the point-in-time store."""
        requested = [_project_to_databento_symbol(symbol) for symbol in (symbols or [])]
        params: dict[str, str] = {
            "dataset": self.config.equity_dataset,
            "schema": self.config.equity_schema,
            "symbols": ",".join(requested) if requested else "ALL_SYMBOLS",
            "start": _iso(start, "start"),
            "end": _iso(end, "end"),
            "encoding": "json",
            "pretty_px": "true",
            "pretty_ts": "true",
            "map_symbols": "true",
        }
        if requested:
            params["stype_in"] = "raw_symbol"

        count = 0
        chunk: list[dict[str, Any]] = []
        for record in self._stream_records(params):
            row = equity_ohlcv_record_to_store_row(
                record,
                source=f"databento:{self.config.equity_dataset}:{self.config.equity_schema}",
            )
            if row is None:
                continue
            chunk.append(row)
            if len(chunk) >= self.config.ingest_chunk_rows:
                count += store.ingest_equity_bars(pd.DataFrame(chunk))
                chunk.clear()
        if chunk:
            count += store.ingest_equity_bars(pd.DataFrame(chunk))
        return count

    def ingest_option_cbbo(
        self,
        store: OptionsResearchStore,
        *,
        underlyings: Iterable[str],
        start: object,
        end: object,
    ) -> int:
        """Stream one-minute consolidated OPRA BBO for selected underlyings."""
        parent_symbols = [f"{_project_to_databento_symbol(symbol)}.OPT" for symbol in underlyings]
        if not parent_symbols:
            return 0
        params = {
            "dataset": self.config.options_dataset,
            "schema": self.config.options_schema,
            "stype_in": "parent",
            "symbols": ",".join(parent_symbols),
            "start": _iso(start, "start"),
            "end": _iso(end, "end"),
            "encoding": "json",
            "pretty_px": "true",
            "pretty_ts": "true",
            "map_symbols": "true",
        }

        count = 0
        chunk: list[dict[str, Any]] = []
        for record in self._stream_records(params):
            row = option_cbbo_record_to_store_row(
                record,
                source=f"databento:{self.config.options_dataset}:{self.config.options_schema}",
            )
            if row is None:
                continue
            chunk.append(row)
            if len(chunk) >= self.config.ingest_chunk_rows:
                count += store.ingest_option_quotes(pd.DataFrame(chunk))
                chunk.clear()
        if chunk:
            count += store.ingest_option_quotes(pd.DataFrame(chunk))
        return count

    def _stream_records(self, params: Mapping[str, str]) -> Iterator[dict[str, Any]]:
        response = self._session.post(
            _HISTORICAL_URL,
            data=dict(params),
            auth=(self._api_key, ""),
            stream=True,
            timeout=self.config.request_timeout_seconds,
        )
        response.raise_for_status()
        for line in response.iter_lines(decode_unicode=True):
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError("Databento returned malformed JSONL") from exc
            if isinstance(payload, dict):
                yield payload


def equity_ohlcv_record_to_store_row(
    record: Mapping[str, Any],
    *,
    source: str,
) -> dict[str, Any] | None:
    """Convert one Databento OHLCV record to the research-store schema."""
    symbol = _record_symbol(record)
    event_ts = _record_timestamp(record, prefer_recv=False)
    if symbol is None or event_ts is None:
        return None
    prices = {name: _finite(record.get(name)) for name in ("open", "high", "low", "close")}
    volume = _finite(record.get("volume"))
    if any(value is None for value in prices.values()) or volume is None:
        return None

    available_at = _equity_bar_available_at(event_ts)
    return {
        "symbol": _databento_to_project_symbol(symbol),
        "event_ts": event_ts,
        "interval": "1D",
        "open": prices["open"],
        "high": prices["high"],
        "low": prices["low"],
        "close": prices["close"],
        "volume": volume,
        "source": source,
        "available_at": available_at,
    }


def option_cbbo_record_to_store_row(
    record: Mapping[str, Any],
    *,
    source: str,
) -> dict[str, Any] | None:
    """Convert one mapped Databento CBBO record to the research-store schema."""
    symbol = _record_symbol(record)
    if symbol is None:
        return None
    try:
        contract = parse_osi_symbol(symbol)
    except ValueError:
        return None

    event_ts = _record_timestamp(record, prefer_recv=True)
    bid = _finite(record.get("bid_px_00"))
    ask = _finite(record.get("ask_px_00"))
    if event_ts is None or bid is None or ask is None or bid < 0 or ask < bid:
        return None

    return {
        "contract_symbol": _compact_osi_symbol(contract),
        "underlying": contract.project_underlying,
        "event_ts": event_ts,
        "expiration": contract.expiration,
        "strike": contract.strike,
        "option_type": contract.option_type,
        "bid": bid,
        "ask": ask,
        "last": _finite(record.get("price")),
        "volume": None,
        "open_interest": None,
        "implied_volatility": None,
        "source": source,
        "available_at": event_ts,
    }


def parse_osi_symbol(raw_symbol: str) -> OSIContract:
    """Parse an OCC/OSI equity-option symbol from Databento raw symbology."""
    raw = str(raw_symbol or "").rstrip()
    if len(raw) < 16:
        raise ValueError("OSI symbol is too short")
    suffix = raw[-15:]
    root = raw[:-15].strip()
    if not root or len(suffix) != 15:
        raise ValueError("OSI symbol has no root")

    date_token = suffix[:6]
    option_token = suffix[6]
    strike_token = suffix[7:]
    if not date_token.isdigit() or option_token not in {"C", "P"} or not strike_token.isdigit():
        raise ValueError("OSI symbol has invalid expiry/type/strike fields")

    expiry_date = datetime.strptime(date_token, "%y%m%d").date()
    expiration = datetime.combine(expiry_date, time(16, 0), tzinfo=_ET).astimezone(_UTC)
    strike = int(strike_token) / 1000.0
    if strike <= 0:
        raise ValueError("OSI strike must be positive")

    return OSIContract(
        raw_symbol=raw,
        root=root,
        project_underlying=_databento_to_project_symbol(root),
        expiration=expiration,
        option_type="call" if option_token == "C" else "put",
        strike=strike,
    )


def _record_symbol(record: Mapping[str, Any]) -> str | None:
    value = record.get("symbol") or record.get("raw_symbol")
    text = str(value or "").rstrip()
    return text or None


def _record_timestamp(record: Mapping[str, Any], *, prefer_recv: bool) -> datetime | None:
    header = record.get("hd") if isinstance(record.get("hd"), Mapping) else {}
    candidates = (
        (record.get("ts_recv"), header.get("ts_event"), record.get("ts_event"))
        if prefer_recv
        else (header.get("ts_event"), record.get("ts_event"), record.get("ts_recv"))
    )
    for value in candidates:
        parsed = _timestamp_or_none(value)
        if parsed is not None:
            return parsed
    return None


def _timestamp_or_none(value: object) -> datetime | None:
    if value is None:
        return None
    parsed = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(parsed):
        return None
    return pd.Timestamp(parsed).to_pydatetime()


def _equity_bar_available_at(event_ts: datetime) -> datetime:
    """Daily OHLCV is knowable no earlier than the regular-session close."""
    session_date: date = event_ts.astimezone(_UTC).date()
    market_close = datetime.combine(session_date, time(16, 0), tzinfo=_ET).astimezone(_UTC)
    return max(event_ts.astimezone(_UTC), market_close)


def _databento_to_project_symbol(symbol: str) -> str:
    normalized = str(symbol).strip().upper().replace(".", "-")
    if normalized.endswith(".US"):
        return normalized
    return f"{normalized}.US"


def _project_to_databento_symbol(symbol: str) -> str:
    normalized = str(symbol or "").strip().upper()
    if normalized.endswith(".US"):
        normalized = normalized[:-3]
    return normalized.replace("-", ".")


def _compact_osi_symbol(contract: OSIContract) -> str:
    date_token = contract.expiration.astimezone(_ET).strftime("%y%m%d")
    option_token = "C" if contract.option_type == "call" else "P"
    strike_token = f"{int(round(contract.strike * 1000)):08d}"
    root = contract.root.replace(" ", "")
    return f"{root}{date_token}{option_token}{strike_token}"


def _finite(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if pd.isna(number) or number in (float("inf"), float("-inf")):
        return None
    return number


def _iso(value: object, name: str) -> str:
    parsed = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(parsed):
        raise ValueError(f"{name} must be a valid timestamp")
    return pd.Timestamp(parsed).isoformat()


__all__ = [
    "DatabentoHistoricalAdapter",
    "DatabentoHistoricalConfig",
    "OSIContract",
    "equity_ohlcv_record_to_store_row",
    "option_cbbo_record_to_store_row",
    "parse_osi_symbol",
]
