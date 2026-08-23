"""Read-only historical IV/Greeks overlay for point-in-time replay.

The overlay aligns volatility observations to each quote by contract and event
time. A quote can see only a volatility observation whose event_ts is at or
before that quote's event_ts and whose available_at was already knowable at the
research as-of timestamp.
"""

from __future__ import annotations

from typing import Any, Iterable

import pandas as pd

from .historical_volatility_store import HistoricalOptionVolatilityStore


class VolatilityAwareResearchStoreView:
    """Compose a quote-capable research-store view with a volatility store."""

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
        contract_values = [str(item) for item in quotes["contract_symbol"].dropna().unique()]
        quote_start = pd.to_datetime(quotes["event_ts"], utc=True, errors="coerce").min()
        quote_end = pd.to_datetime(quotes["event_ts"], utc=True, errors="coerce").max()
        if pd.isna(quote_start) or pd.isna(quote_end):
            return quotes
        observations = self.volatility_store.observations_asof(
            as_of=as_of,
            start=quote_start,
            end=quote_end,
            contracts=contract_values,
        )
        if observations.empty:
            return _ensure_volatility_columns(quotes)
        return _overlay(quotes, observations)

    def option_definitions_asof(self, *args: Any, **kwargs: Any) -> pd.DataFrame:
        return self.base_store.option_definitions_asof(*args, **kwargs)

    def option_statistics_asof(self, *args: Any, **kwargs: Any) -> pd.DataFrame:
        return self.base_store.option_statistics_asof(*args, **kwargs)

    def counts(self) -> dict[str, Any]:
        base = dict(self.base_store.counts())
        base["historical_volatility"] = self.volatility_store.counts()
        return base


def _overlay(quotes: pd.DataFrame, observations: pd.DataFrame) -> pd.DataFrame:
    q = quotes.copy()
    q["event_ts"] = pd.to_datetime(q["event_ts"], utc=True, errors="coerce")
    q = q.dropna(subset=["event_ts", "contract_symbol"])
    q["_quote_order"] = range(len(q))

    v = observations.copy()
    v["event_ts"] = pd.to_datetime(v["event_ts"], utc=True, errors="coerce")
    v["available_at"] = pd.to_datetime(v["available_at"], utc=True, errors="coerce")
    v = v.dropna(subset=["event_ts", "contract_symbol"])

    pieces: list[pd.DataFrame] = []
    for contract, quote_group in q.groupby("contract_symbol", sort=False):
        vol_group = v[v["contract_symbol"] == contract].copy()
        quote_group = quote_group.sort_values("event_ts")
        if vol_group.empty:
            pieces.append(_ensure_volatility_columns(quote_group))
            continue
        vol_group = vol_group.sort_values("event_ts").rename(
            columns={
                "implied_volatility": "_vol_implied_volatility",
                "delta": "_vol_delta",
                "gamma": "_vol_gamma",
                "theta": "_vol_theta",
                "vega": "_vol_vega",
                "rho": "_vol_rho",
                "underlying_price": "_vol_underlying_price",
                "risk_free_rate": "_vol_risk_free_rate",
                "dividend_yield": "_vol_dividend_yield",
                "model": "_vol_model",
                "source": "_vol_source",
                "available_at": "_vol_available_at",
                "vendor_observation_id": "_vol_vendor_observation_id",
            }
        )
        merged = pd.merge_asof(
            quote_group,
            vol_group[
                [
                    "event_ts",
                    "_vol_implied_volatility",
                    "_vol_delta",
                    "_vol_gamma",
                    "_vol_theta",
                    "_vol_vega",
                    "_vol_rho",
                    "_vol_underlying_price",
                    "_vol_risk_free_rate",
                    "_vol_dividend_yield",
                    "_vol_model",
                    "_vol_source",
                    "_vol_available_at",
                    "_vol_vendor_observation_id",
                ]
            ],
            on="event_ts",
            direction="backward",
            allow_exact_matches=True,
        )
        pieces.append(_fill_from_volatility(merged))

    output = pd.concat(pieces, ignore_index=True) if pieces else _ensure_volatility_columns(q)
    output = output.sort_values("_quote_order").drop(columns=["_quote_order"], errors="ignore")
    return output.reset_index(drop=True)


def _fill_from_volatility(frame: pd.DataFrame) -> pd.DataFrame:
    output = frame.copy()
    for field, source_field in (
        ("implied_volatility", "_vol_implied_volatility"),
        ("delta", "_vol_delta"),
        ("gamma", "_vol_gamma"),
        ("theta", "_vol_theta"),
        ("vega", "_vol_vega"),
        ("rho", "_vol_rho"),
    ):
        if field not in output.columns:
            output[field] = pd.NA
        native = pd.to_numeric(output[field], errors="coerce")
        fallback = pd.to_numeric(output.get(source_field), errors="coerce")
        output[field] = native.where(native.notna(), fallback)

    output["volatility_source"] = output.get("_vol_source")
    output["volatility_model"] = output.get("_vol_model")
    output["volatility_available_at"] = output.get("_vol_available_at")
    output["volatility_vendor_observation_id"] = output.get("_vol_vendor_observation_id")
    output["volatility_underlying_price"] = output.get("_vol_underlying_price")
    output["volatility_risk_free_rate"] = output.get("_vol_risk_free_rate")
    output["volatility_dividend_yield"] = output.get("_vol_dividend_yield")
    return output.drop(
        columns=[column for column in output.columns if column.startswith("_vol_")],
        errors="ignore",
    )


def _ensure_volatility_columns(frame: pd.DataFrame) -> pd.DataFrame:
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


__all__ = ["VolatilityAwareResearchStoreView"]
