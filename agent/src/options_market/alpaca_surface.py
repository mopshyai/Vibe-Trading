"""Read-only Alpaca adapter for provider-independent volatility-surface analysis.

This module intentionally keeps the surface math in ``surface.py``. It reuses
the existing focused Alpaca reader's GET-only contract/snapshot primitives and
normalizes them into a vendor-neutral surface row shape.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from .alpaca_current import AlpacaCurrentOptionsReader
from .surface import VolatilitySurfaceConfig, analyze_volatility_surface

UTC = timezone.utc


def fetch_alpaca_volatility_surface(
    reader: AlpacaCurrentOptionsReader,
    underlying: str,
    *,
    now: datetime | None = None,
    realized_vol_pct: float | None = None,
    config: VolatilitySurfaceConfig | None = None,
) -> dict[str, Any]:
    """Fetch both call and put surfaces for one focus symbol using GETs only."""
    clean = str(underlying or "").strip().upper().removesuffix(".US")
    if not clean:
        raise ValueError("underlying is required")
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    reference = reference.astimezone(UTC)

    calls = reader._contracts(clean, "call", reference.date())  # noqa: SLF001 - same adapter package
    puts = reader._contracts(clean, "put", reference.date())  # noqa: SLF001 - same adapter package
    contracts = [*calls, *puts]
    symbols = [str(row.get("symbol") or "").strip().upper() for row in contracts]
    snapshots = reader._snapshots(symbols)  # noqa: SLF001 - shared GET-only primitive
    spot = reader._underlying_mid(clean)  # noqa: SLF001 - shared GET-only primitive
    if spot is None:
        return {
            "status": "insufficient",
            "underlying": clean,
            "feed": reader.config.option_feed,
            "execution_grade_feed": reader.config.option_feed == "opra",
            "observed_at": reference.isoformat(),
            "contracts_fetched": len(contracts),
            "snapshots_fetched": len(snapshots),
            "warnings": ["underlying_quote_unavailable"],
        }

    observations: list[dict[str, Any]] = []
    for contract in contracts:
        symbol = str(contract.get("symbol") or "").strip().upper()
        snapshot = snapshots.get(symbol)
        if not symbol or not isinstance(snapshot, Mapping):
            continue
        row = _surface_observation(contract, snapshot, now=reference)
        if row is not None:
            observations.append(row)

    report = analyze_volatility_surface(
        observations,
        spot=float(spot),
        realized_vol_pct=realized_vol_pct,
        config=config,
    )
    return {
        **report,
        "underlying": clean,
        "feed": reader.config.option_feed,
        "execution_grade_feed": reader.config.option_feed == "opra",
        "observed_at": reference.isoformat(),
        "contracts_fetched": len(contracts),
        "snapshots_fetched": len(snapshots),
        "surface_rows": len(observations),
        "source": "alpaca_current_options_surface",
        "warning": "Read-only surface research; no broker order path is used.",
    }


def _surface_observation(
    contract: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    *,
    now: datetime,
) -> dict[str, Any] | None:
    if not bool(contract.get("tradable", True)):
        return None
    option_type = str(contract.get("type") or contract.get("option_type") or "").strip().lower()
    if option_type not in {"call", "put"}:
        return None
    expiration = str(contract.get("expiration_date") or contract.get("expiration") or "")[:10]
    try:
        expiry = datetime.fromisoformat(expiration).date()
    except ValueError:
        return None
    dte = (expiry - now.date()).days
    if dte <= 0:
        return None

    quote = snapshot.get("latestQuote") or snapshot.get("latest_quote") or snapshot.get("quote")
    quote = quote if isinstance(quote, Mapping) else {}
    greeks = snapshot.get("greeks") if isinstance(snapshot.get("greeks"), Mapping) else {}
    iv = _positive(snapshot.get("impliedVolatility") or snapshot.get("implied_volatility"))
    strike = _positive(contract.get("strike_price") or contract.get("strike"))
    if iv is None or strike is None:
        return None
    return {
        "contract_symbol": str(contract.get("symbol") or "").strip().upper(),
        "option_type": option_type,
        "strike": strike,
        "expiration": expiration,
        "dte": dte,
        "bid": _nonnegative(quote.get("bp") or quote.get("bid_price") or quote.get("bid")),
        "ask": _positive(quote.get("ap") or quote.get("ask_price") or quote.get("ask")),
        "implied_volatility": iv,
        "delta": _finite(greeks.get("delta")),
        "gamma": _finite(greeks.get("gamma")),
        "theta": _finite(greeks.get("theta")),
        "vega": _finite(greeks.get("vega")),
        "open_interest": _integer(contract.get("open_interest"), 0),
    }


def _finite(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _positive(value: object) -> float | None:
    number = _finite(value)
    return number if number is not None and number > 0 else None


def _nonnegative(value: object) -> float | None:
    number = _finite(value)
    return number if number is not None and number >= 0 else None


def _integer(value: object, default: int = 0) -> int:
    number = _finite(value)
    if number is None or int(number) != number:
        return default
    return int(number)


__all__ = ["fetch_alpaca_volatility_surface"]
