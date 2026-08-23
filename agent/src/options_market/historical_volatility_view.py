"""Production-facing historical IV/Greeks overlay for point-in-time replay.

A volatility observation can enrich a quote only when:
1. the observation was available by the research `as_of` timestamp; and
2. the observation event occurred at or before that individual quote event.

The volatility lookup intentionally reaches before the quote lookback start. A
prior daily/EOD IV observation may be the latest valid observation for a later
intraday quote; limiting the lookup to the quote window would incorrectly turn
that into missing data.
"""

from __future__ import annotations

from typing import Any, Iterable

import pandas as pd

from .historical_volatility_store import HistoricalOptionVolatilityStore


class HistoricalVolatilityResearchStoreView:
    """Overlay point-in-time IV/Greeks on an existing quote-capable store view."""

    def __init__(self, base_store: Any, volatility_store: HistoricalOptionVolatilityStore) -> None:
        self.base_store = base_store
        self.volatility_store = volatility_store

    def equity_bars_asof(self, *args: Any, **kwargs: Any) -> pd.DataFrame:
        return self.base_store.equity_bars_asof(*args, **kwargs)

    def equity_frames_asof(self, *args: Any, **kwargs: Any) -> dict[str, pd.DataFrame]:
        return self.base_store.equity_frames_asof(*args, **kwargs)

    def option_quotes_asof(
        self,
        *,
        as_of: object,
        start: object | None = None,
        end: object | None = None,
        underlyings: Iterable[str] | None = None,
        contracts: Iterable[str] | None = None,
    ) -> pd.DataFrame:
        quotes = self.base_store.option_quotes_asof(
            as_of=as_of,
            start=start,
            end=end,
            underlyings=underlyings,
            contracts=contracts,
        )
        if quotes.empty:
            return quotes
        quote_times = pd.to_datetime(quotes["event_ts"], utc=True, errors="coerce")
        quote_end = quote_times.max()
        if pd.isna(quote_end):
            return _ensure_overlay_columns(quotes)
        contract_values = [str(item) for item in quotes["contract_symbol"].dropna().unique()]
        observations = self.volatility_store.observations_asof(
            as_of=as_of,
            end=quote_end,
            contracts=contract_values,
        )
        if observations.empty:
            return _ensure_overlay_columns(quotes)
        return _overlay_by_contract_and_time(quotes, observations)

    def option_definitions_asof(self, *args: Any, **kwargs: Any) -> pd.DataFrame:
        return self.base_store.option_definitions_asof(*args, **kwargs)

    def option_statistics_asof(self, *args: Any, **kwargs: Any) -> pd.DataFrame:
        return self.base_store.option_statistics_asof(*args, **kwargs)

    def counts(self) -> dict[str, Any]:
        output = dict(self.base_store.counts())
        output["historical_volatility"] = self.volatility_store.counts()
        return output


def _overlay_by_contract_and_time(quotes: pd.DataFrame, observations: pd.DataFrame) -> pd.DataFrame:
    q = quotes.copy()
    q["event_ts"] = pd.to_datetime(q["event_ts"], utc=True, errors="coerce")
    q = q.dropna(subset=["event_ts", "contract_symbol"])
    q["_original_order"] = range(len(q))

    v = observations.copy()
    v["event_ts"] = pd.to_datetime(v["event_ts"], utc=True, errors="coerce")
    v["available_at"] = pd.to_datetime(v["available_at"], utc=True, errors="coerce")
    v = v.dropna(subset=["event_ts", "contract_symbol"])

    pieces: list[pd.DataFrame] = []
    for contract, quote_group in q.groupby("contract_symbol", sort=False):
        quote_group = quote_group.sort_values("event_ts")
        vol_group = v[v["contract_symbol"] == contract].copy().sort_values("event_ts")
        if vol_group.empty:
            pieces.append(_ensure_overlay_columns(quote_group))
            continue
        vol_group = vol_group.rename(
            columns={
                "implied_volatility": "_overlay_iv",
                "delta": "_overlay_delta",
                "gamma": "_overlay_gamma",
                "theta": "_overlay_theta",
                "vega": "_overlay_vega",
                "rho": "_overlay_rho",
                "underlying_price": "_overlay_underlying_price",
                "risk_free_rate": "_overlay_risk_free_rate",
                "dividend_yield": "_overlay_dividend_yield",
                "model": "_overlay_model",
                "source": "_overlay_source",
                "available_at": "_overlay_available_at",
                "vendor_observation_id": "_overlay_vendor_observation_id",
            }
        )
        merged = pd.merge_asof(
            quote_group,
            vol_group[
                [
                    "event_ts",
                    "_overlay_iv",
                    "_overlay_delta",
                    "_overlay_gamma",
                    "_overlay_theta",
                    "_overlay_vega",
                    "_overlay_rho",
                    "_overlay_underlying_price",
                    "_overlay_risk_free_rate",
                    "_overlay_dividend_yield",
                    "_overlay_model",
                    "_overlay_source",
                    "_overlay_available_at",
                    "_overlay_vendor_observation_id",
                ]
            ],
            on="event_ts",
            direction="backward",
            allow_exact_matches=True,
        )
        pieces.append(_fill_missing_quote_fields(merged))

    if not pieces:
        return _ensure_overlay_columns(q)
    output = pd.concat(pieces, ignore_index=True)
    output = output.sort_values("_original_order").drop(columns=["_original_order"], errors="ignore")
    return output.reset_index(drop=True)


def _fill_missing_quote_fields(frame: pd.DataFrame) -> pd.DataFrame:
    output = frame.copy()
    for field, fallback_field in (
        ("implied_volatility", "_overlay_iv"),
        ("delta", "_overlay_delta"),
        ("gamma", "_overlay_gamma"),
        ("theta", "_overlay_theta"),
        ("vega", "_overlay_vega"),
        ("rho", "_overlay_rho"),
    ):
        if field not in output.columns:
            output[field] = pd.NA
        native = pd.to_numeric(output[field], errors="coerce")
        fallback = pd.to_numeric(output.get(fallback_field), errors="coerce")
        output[field] = native.where(native.notna(), fallback)

    output["volatility_source"] = output.get("_overlay_source")
    output["volatility_model"] = output.get("_overlay_model")
    output["volatility_available_at"] = output.get("_overlay_available_at")
    output["volatility_vendor_observation_id"] = output.get("_overlay_vendor_observation_id")
    output["volatility_underlying_price"] = output.get("_overlay_underlying_price")
    output["volatility_risk_free_rate"] = output.get("_overlay_risk_free_rate")
    output["volatility_dividend_yield"] = output.get("_overlay_dividend_yield")
    return output.drop(
        columns=[column for column in output.columns if column.startswith("_overlay_")],
        errors="ignore",
    )


def _ensure_overlay_columns(frame: pd.DataFrame) -> pd.DataFrame:
    output = frame.copy()
    for column in ("delta", "gamma", "theta", "vega", "rho"):
        if column not in output.columns:
            output[column] = pd.NA
    for column in (
        "volatility_source",
        "volatility_model",
        "volatility_available_at",
        "volatility_vendor_observation_id",
        "volatility_underlying_price",
        "volatility_risk_free_rate",
        "volatility_dividend_yield",
    ):
        if column not in output.columns:
            output[column] = None
    return output


__all__ = ["HistoricalVolatilityResearchStoreView"]
