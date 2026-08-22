"""Bulk, read-only Alpaca U.S. equity daily-bar adapter.

This adapter updates the current edge of the point-in-time equity store without
requiring a paid historical Databento request on every trading day. It uses the
multi-symbol Alpaca `/v2/stocks/bars` endpoint, preserves the selected feed in
provenance, and stamps `available_at` with the actual refresh time.

Rows fetched here are suitable for *current/future* research as of the refresh
time. They are not retroactively made available to older replays; historical
backfill retains its own availability semantics.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping
from urllib.parse import urlencode

import pandas as pd
import requests

from src.trading import tap_forward
from src.trading.connectors.alpaca import sdk as alpaca_sdk

UTC = timezone.utc


class AlpacaBulkEquityReader:
    def __init__(
        self,
        *,
        alpaca_config: alpaca_sdk.AlpacaConfig,
        session: requests.Session | None = None,
        request_timeout_seconds: float = 30.0,
        symbol_batch_size: int = 200,
        max_pages_per_batch: int = 200,
    ) -> None:
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if not 1 <= symbol_batch_size <= 1000:
            raise ValueError("symbol_batch_size must be between 1 and 1000")
        if max_pages_per_batch < 1:
            raise ValueError("max_pages_per_batch must be at least 1")
        self.alpaca_config = alpaca_config
        self.session = session or requests.Session()
        self.request_timeout_seconds = request_timeout_seconds
        self.symbol_batch_size = symbol_batch_size
        self.max_pages_per_batch = max_pages_per_batch

    def fetch_daily_bars(
        self,
        symbols: Iterable[str],
        *,
        start: object,
        end: object,
        feed: str = "sip",
        adjustment: str = "split",
        observed_at: datetime | None = None,
    ) -> pd.DataFrame:
        """Fetch split-adjusted 1D bars for many U.S. symbols."""
        clean = _symbols(symbols)
        if feed not in {"sip", "iex"}:
            raise ValueError("feed must be sip or iex")
        if adjustment not in {"raw", "split", "dividend", "spin-off", "all"}:
            raise ValueError("unsupported adjustment")
        if not clean:
            return _empty_frame()
        available_at = _aware(observed_at or datetime.now(UTC), "observed_at")
        rows: list[dict[str, Any]] = []
        for offset in range(0, len(clean), self.symbol_batch_size):
            batch = clean[offset : offset + self.symbol_batch_size]
            rows.extend(
                self._batch(
                    batch,
                    start=start,
                    end=end,
                    feed=feed,
                    adjustment=adjustment,
                    available_at=available_at,
                )
            )
        if not rows:
            return _empty_frame()
        frame = pd.DataFrame(rows)
        return frame.sort_values(["symbol", "event_ts"]).reset_index(drop=True)

    def _batch(
        self,
        symbols: list[str],
        *,
        start: object,
        end: object,
        feed: str,
        adjustment: str,
        available_at: datetime,
    ) -> list[dict[str, Any]]:
        params: dict[str, object] = {
            "symbols": ",".join(symbols),
            "timeframe": "1Day",
            "start": _query_time(start),
            "end": _query_time(end),
            "limit": 10_000,
            "adjustment": adjustment,
            "feed": feed,
            "sort": "asc",
        }
        output: list[dict[str, Any]] = []
        page_token: str | None = None
        for page in range(self.max_pages_per_batch):
            if page_token:
                params["page_token"] = page_token
            elif "page_token" in params:
                del params["page_token"]
            payload = self._get_json(params)
            bars = payload.get("bars") if isinstance(payload, Mapping) else None
            if isinstance(bars, Mapping):
                for symbol, values in bars.items():
                    if not isinstance(values, list):
                        continue
                    for raw in values:
                        if not isinstance(raw, Mapping):
                            continue
                        parsed = _bar_row(
                            symbol,
                            raw,
                            source=f"alpaca:{feed}:1D:{adjustment}",
                            available_at=available_at,
                        )
                        if parsed is not None:
                            output.append(parsed)
            page_token = str(payload.get("next_page_token") or "").strip() if isinstance(payload, Mapping) else ""
            if not page_token:
                break
        else:
            raise RuntimeError(
                f"Alpaca equity-bar pagination exceeded {self.max_pages_per_batch} pages for batch"
            )
        return output

    def _get_json(self, params: Mapping[str, object]) -> Mapping[str, Any]:
        url = f"{alpaca_sdk.DATA_HOST}/v2/stocks/bars"
        query = urlencode({key: value for key, value in params.items() if value not in (None, "")})
        target = f"{url}?{query}"
        if tap_forward.tap_enabled():
            payload = alpaca_sdk._read_via_tap(target)  # noqa: SLF001 - shared credential-isolated GET path
            if not isinstance(payload, Mapping):
                raise RuntimeError("Alpaca/TAP returned a non-object response")
            return payload
        if not self.alpaca_config.api_key or not self.alpaca_config.secret_key:
            raise alpaca_sdk.AlpacaConfigError("Alpaca market-data read requires configured credentials")
        response = self.session.get(
            url,
            params={key: value for key, value in params.items() if value not in (None, "")},
            headers={
                "APCA-API-KEY-ID": self.alpaca_config.api_key,
                "APCA-API-SECRET-KEY": self.alpaca_config.secret_key,
            },
            timeout=self.request_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise RuntimeError("Alpaca returned a non-object response")
        return payload


def _bar_row(
    symbol: object,
    raw: Mapping[str, Any],
    *,
    source: str,
    available_at: datetime,
) -> dict[str, Any] | None:
    event_ts = pd.to_datetime(raw.get("t") or raw.get("timestamp"), utc=True, errors="coerce")
    if pd.isna(event_ts):
        return None
    values: dict[str, float] = {}
    for output, keys in {
        "open": ("o", "open"),
        "high": ("h", "high"),
        "low": ("l", "low"),
        "close": ("c", "close"),
        "volume": ("v", "volume"),
    }.items():
        value = _number(*(raw.get(key) for key in keys))
        if value is None or (output != "volume" and value <= 0) or (output == "volume" and value < 0):
            return None
        values[output] = value
    clean = str(symbol or "").strip().upper()
    if not clean:
        return None
    return {
        "symbol": clean,
        "event_ts": pd.Timestamp(event_ts).to_pydatetime(),
        "interval": "1D",
        **values,
        "source": source,
        "available_at": available_at,
    }


def _symbols(values: Iterable[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for raw in values:
        symbol = str(raw or "").strip().upper()
        if symbol.endswith(".US"):
            symbol = symbol[:-3]
        if symbol and symbol not in seen:
            seen.add(symbol)
            output.append(symbol)
    return output


def _query_time(value: object) -> str:
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"invalid datetime value: {value!r}")
    return pd.Timestamp(parsed).isoformat()


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _number(*values: object) -> float | None:
    for value in values:
        if value is None or isinstance(value, bool):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if number == number and abs(number) != float("inf"):
            return number
    return None


def _empty_frame() -> pd.DataFrame:
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
        ]
    )


__all__ = ["AlpacaBulkEquityReader"]
