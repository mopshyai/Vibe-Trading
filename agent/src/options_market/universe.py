"""U.S. listed-equity universe helpers for the options research engine.

Nasdaq Trader publishes daily symbol-directory files for Nasdaq and other U.S.
venues. Parsing is kept separate from fetching so unit tests and backtests never
need network access.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from io import StringIO
from typing import Iterable

import pandas as pd
import requests

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

_EXCHANGE_NAMES = {
    "Q": "NASDAQ",
    "N": "NYSE",
    "A": "NYSE American",
    "P": "NYSE Arca",
    "Z": "Cboe BZX",
    "V": "IEX",
}


@dataclass(frozen=True)
class USListing:
    symbol: str
    project_symbol: str
    security_name: str
    exchange: str
    etf: bool


def parse_symbol_directory(
    nasdaq_text: str,
    other_text: str,
    *,
    include_etfs: bool = True,
    exchanges: Iterable[str] | None = None,
) -> list[USListing]:
    """Parse Nasdaq Trader symbol-directory text into a clean U.S. universe."""
    allowed = {item.upper() for item in exchanges} if exchanges is not None else None
    rows: list[USListing] = []

    for record in _records(nasdaq_text):
        if record.get("Symbol") in {None, "File Creation Time"}:
            continue
        if str(record.get("Test Issue", "N")).upper() != "N":
            continue
        symbol = _clean_symbol(record.get("Symbol"))
        if symbol is None:
            continue
        etf = str(record.get("ETF", "N")).upper() == "Y"
        if etf and not include_etfs:
            continue
        exchange = "NASDAQ"
        if allowed is not None and exchange.upper() not in allowed:
            continue
        rows.append(
            USListing(
                symbol=symbol,
                project_symbol=f"{symbol}.US",
                security_name=str(record.get("Security Name") or "").strip(),
                exchange=exchange,
                etf=etf,
            )
        )

    for record in _records(other_text):
        if record.get("ACT Symbol") in {None, "File Creation Time"}:
            continue
        if str(record.get("Test Issue", "N")).upper() != "N":
            continue
        symbol = _clean_symbol(record.get("NASDAQ Symbol") or record.get("ACT Symbol"))
        if symbol is None:
            continue
        etf = str(record.get("ETF", "N")).upper() == "Y"
        if etf and not include_etfs:
            continue
        exchange_code = str(record.get("Exchange") or "").strip().upper()
        exchange = _EXCHANGE_NAMES.get(exchange_code, exchange_code or "OTHER")
        if allowed is not None and exchange.upper() not in allowed:
            continue
        rows.append(
            USListing(
                symbol=symbol,
                project_symbol=f"{symbol}.US",
                security_name=str(record.get("Security Name") or "").strip(),
                exchange=exchange,
                etf=etf,
            )
        )

    deduped: dict[str, USListing] = {}
    for listing in rows:
        deduped.setdefault(listing.project_symbol, listing)
    return sorted(deduped.values(), key=lambda item: item.project_symbol)


def fetch_symbol_directory(timeout: float = 20.0) -> tuple[str, str]:
    """Fetch the two public Nasdaq Trader universe files."""
    headers = {"User-Agent": "Vibe-Trading research universe loader"}
    nasdaq = requests.get(NASDAQ_LISTED_URL, headers=headers, timeout=timeout)
    nasdaq.raise_for_status()
    other = requests.get(OTHER_LISTED_URL, headers=headers, timeout=timeout)
    other.raise_for_status()
    return nasdaq.text, other.text


def fetch_us_listed_universe(
    *,
    include_etfs: bool = True,
    exchanges: Iterable[str] | None = None,
    timeout: float = 20.0,
) -> list[USListing]:
    """Fetch and parse the current U.S. listed universe."""
    nasdaq_text, other_text = fetch_symbol_directory(timeout=timeout)
    return parse_symbol_directory(
        nasdaq_text,
        other_text,
        include_etfs=include_etfs,
        exchanges=exchanges,
    )


def universe_payload(listings: list[USListing]) -> list[dict[str, object]]:
    """Return JSON-friendly rows for logging, caching, or CLI output."""
    return [asdict(listing) for listing in listings]


def _records(text: str) -> list[dict[str, object]]:
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    frame = pd.read_csv(StringIO(cleaned), sep="|", dtype=str, keep_default_na=False)
    return frame.to_dict(orient="records")


def _clean_symbol(value: object) -> str | None:
    symbol = str(value or "").strip().upper()
    if not symbol or symbol.startswith("FILE CREATION TIME"):
        return None
    symbol = symbol.replace("$", "-")
    if any(character.isspace() for character in symbol):
        return None
    return symbol


__all__ = [
    "NASDAQ_LISTED_URL",
    "OTHER_LISTED_URL",
    "USListing",
    "fetch_symbol_directory",
    "fetch_us_listed_universe",
    "parse_symbol_directory",
    "universe_payload",
]
