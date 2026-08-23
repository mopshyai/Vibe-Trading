"""Databento OPRA option-reference/statistics ingestion.

This adapter extends the historical HTTP reader with point-in-time option
metadata needed by replay: instrument definitions, published open interest and
completed-session daily volume. It never derives implied volatility.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import pandas as pd

from .databento_adapter import (
    DatabentoHistoricalAdapter,
    _number,
    _utc_datetime,
    parent_option_symbol,
    parse_osi_symbol,
)
from .store import OptionsResearchStore

UTC = timezone.utc
ET = ZoneInfo("America/New_York")


class DatabentoOptionMetadataAdapter(DatabentoHistoricalAdapter):
    """Read OPRA definitions/open-interest/daily-volume into the PIT store."""

    def ingest_option_definitions(
        self,
        underlying: str,
        *,
        start: datetime,
        end: datetime,
        store: OptionsResearchStore,
    ) -> int:
        parent = parent_option_symbol(underlying)
        params = {
            "dataset": self.config.options_dataset,
            "schema": "definition",
            "symbols": parent,
            "stype_in": "parent",
            "stype_out": "raw_symbol",
            "start": _iso_utc(start),
            "end": _iso_utc(end),
            "encoding": "json",
        }
        source = f"databento:{self.config.options_dataset}:definition"
        rows: list[dict[str, Any]] = []
        count = 0
        for record in self._stream_jsonl(params):
            row = _definition_row(record, source=source)
            if row is None:
                continue
            rows.append(row)
            if len(rows) >= self.config.chunk_size:
                count += store.ingest_option_definitions(pd.DataFrame(rows))
                rows.clear()
        if rows:
            count += store.ingest_option_definitions(pd.DataFrame(rows))
        return count

    def ingest_option_open_interest(
        self,
        underlying: str,
        *,
        start: datetime,
        end: datetime,
        store: OptionsResearchStore,
    ) -> int:
        parent = parent_option_symbol(underlying)
        params = {
            "dataset": self.config.options_dataset,
            "schema": "statistics",
            "symbols": parent,
            "stype_in": "parent",
            "stype_out": "raw_symbol",
            "start": _iso_utc(start),
            "end": _iso_utc(end),
            "encoding": "json",
        }
        source = f"databento:{self.config.options_dataset}:statistics:open_interest"
        rows: list[dict[str, Any]] = []
        count = 0
        for record in self._stream_jsonl(params):
            row = _open_interest_row(record, source=source)
            if row is None:
                continue
            rows.append(row)
            if len(rows) >= self.config.chunk_size:
                count += store.ingest_option_statistics(pd.DataFrame(rows))
                rows.clear()
        if rows:
            count += store.ingest_option_statistics(pd.DataFrame(rows))
        return count

    def ingest_option_daily_volume(
        self,
        underlying: str,
        *,
        start: datetime,
        end: datetime,
        store: OptionsResearchStore,
    ) -> int:
        parent = parent_option_symbol(underlying)
        params = {
            "dataset": self.config.options_dataset,
            "schema": "ohlcv-1d",
            "symbols": parent,
            "stype_in": "parent",
            "stype_out": "raw_symbol",
            "start": _iso_utc(start),
            "end": _iso_utc(end),
            "encoding": "json",
        }
        source = f"databento:{self.config.options_dataset}:ohlcv-1d:daily_volume"
        rows: list[dict[str, Any]] = []
        count = 0
        for record in self._stream_jsonl(params):
            row = _daily_volume_row(record, source=source)
            if row is None:
                continue
            rows.append(row)
            if len(rows) >= self.config.chunk_size:
                count += store.ingest_option_statistics(pd.DataFrame(rows))
                rows.clear()
        if rows:
            count += store.ingest_option_statistics(pd.DataFrame(rows))
        return count


def _definition_row(record: Mapping[str, Any], *, source: str) -> dict[str, Any] | None:
    raw = str(record.get("symbol") or record.get("raw_symbol") or "").strip()
    try:
        parsed = parse_osi_symbol(raw)
    except ValueError:
        return None
    event_ts = _first_time(record, "ts_recv", "ts_event", "ts_ref")
    if event_ts is None:
        return None
    available_at = _first_time(record, "ts_recv") or event_ts
    activation = _first_time(record, "activation")
    min_tick = _dbn_price(record.get("min_price_increment"))
    multiplier = _positive_number(
        record.get("original_contract_size")
        or record.get("contract_multiplier")
        or record.get("unit_of_measure_qty")
    )
    return {
        "contract_symbol": parsed.contract_symbol,
        "underlying": parsed.project_underlying,
        "event_ts": event_ts,
        "expiration": datetime.combine(parsed.expiration, time(20, 0), tzinfo=UTC),
        "strike": parsed.strike,
        "option_type": parsed.option_type,
        "activation": activation,
        "min_price_increment": min_tick,
        "contract_multiplier": multiplier,
        "update_action": _text(record.get("security_update_action") or record.get("update_action")),
        "raw_symbol": raw,
        "source": source,
        "available_at": max(event_ts, available_at),
    }


def _open_interest_row(record: Mapping[str, Any], *, source: str) -> dict[str, Any] | None:
    stat_type = str(record.get("stat_type") or "").strip().upper()
    if stat_type not in {"9", "OPEN_INTEREST", "OPEN INTEREST"}:
        return None
    raw = str(record.get("symbol") or record.get("raw_symbol") or "").strip()
    try:
        parsed = parse_osi_symbol(raw)
    except ValueError:
        return None
    value = _nonnegative_number(record.get("quantity"))
    available_at = _first_time(record, "ts_recv", "ts_event")
    if value is None or available_at is None:
        return None
    reference_ts = _first_time(record, "ts_ref")
    return {
        "contract_symbol": parsed.contract_symbol,
        "underlying": parsed.project_underlying,
        "event_ts": available_at,
        "reference_ts": reference_ts,
        "metric": "open_interest",
        "value": value,
        "source": source,
        "available_at": available_at,
    }


def _daily_volume_row(record: Mapping[str, Any], *, source: str) -> dict[str, Any] | None:
    raw = str(record.get("symbol") or record.get("raw_symbol") or "").strip()
    try:
        parsed = parse_osi_symbol(raw)
    except ValueError:
        return None
    reference_ts = _first_time(record, "ts_event", "ts_recv")
    volume = _nonnegative_number(record.get("volume"))
    if reference_ts is None or volume is None:
        return None
    close = _regular_close(reference_ts.date())
    return {
        "contract_symbol": parsed.contract_symbol,
        "underlying": parsed.project_underlying,
        "event_ts": close,
        "reference_ts": reference_ts,
        "metric": "daily_volume",
        "value": volume,
        "source": source,
        "available_at": close,
    }


def _first_time(record: Mapping[str, Any], *fields: str) -> datetime | None:
    for field in fields:
        value = record.get(field)
        if value is None:
            continue
        try:
            return _utc_datetime(value, field)
        except ValueError:
            continue
    return None


def _dbn_price(value: object) -> float | None:
    number = _number(value)
    if number is None or number <= 0:
        return None
    # DBN fixed-price fields use 1e-9 precision. JSON responses may already be
    # converted to regular prices, so scale only obviously fixed-point values.
    if abs(number) >= 10_000_000:
        number /= 1_000_000_000.0
    return number if number > 0 else None


def _positive_number(value: object) -> float | None:
    number = _number(value)
    return number if number is not None and number > 0 else None


def _nonnegative_number(value: object) -> float | None:
    number = _number(value)
    return number if number is not None and number >= 0 else None


def _text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _regular_close(day: date) -> datetime:
    return datetime.combine(day, time(16, 0), tzinfo=ET).astimezone(UTC)


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Databento request timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


__all__ = ["DatabentoOptionMetadataAdapter"]
