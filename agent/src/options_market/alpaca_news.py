"""Read-only Alpaca news ingestion for focused U.S. equity research.

Alpaca's Market Data News API supplies article creation/update timestamps and
symbol tags. This adapter preserves those timestamps, performs only conservative
headline classification, and returns point-in-time rows suitable for
``CatalystEventStore``. Ambiguous headlines remain neutral rather than inventing
directional conviction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Any, Iterable, Mapping
from urllib.parse import urlencode

import requests

from src.trading import tap_forward
from src.trading.connectors.alpaca import sdk as alpaca_sdk

UTC = timezone.utc
_NEWS_URL = f"{alpaca_sdk.DATA_HOST}/v1beta1/news"


@dataclass(frozen=True)
class AlpacaNewsConfig:
    lookback_hours: float = 72.0
    max_pages: int = 10
    page_limit: int = 50
    max_symbols_per_request: int = 50

    def validate(self) -> None:
        if not 0 < self.lookback_hours <= 720:
            raise ValueError("lookback_hours must be > 0 and <= 720")
        if not 1 <= self.max_pages <= 100:
            raise ValueError("max_pages must be between 1 and 100")
        if not 1 <= self.page_limit <= 50:
            raise ValueError("page_limit must be between 1 and 50")
        if not 1 <= self.max_symbols_per_request <= 200:
            raise ValueError("max_symbols_per_request must be between 1 and 200")


class AlpacaNewsReader:
    """Fetch symbol-tagged Alpaca news without broker mutation."""

    def __init__(
        self,
        *,
        config: AlpacaNewsConfig | None = None,
        alpaca_config: alpaca_sdk.AlpacaConfig | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.config = config or AlpacaNewsConfig()
        self.config.validate()
        self.alpaca_config = alpaca_config or alpaca_sdk.load_config()
        self.session = session or requests.Session()

    def fetch(
        self,
        symbols: Iterable[str],
        *,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        reference = now or datetime.now(UTC)
        if reference.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        clean_symbols = _symbols(symbols)
        if not clean_symbols:
            return []
        start = reference.astimezone(UTC) - timedelta(hours=self.config.lookback_hours)
        rows: list[dict[str, Any]] = []
        for batch in _chunks(clean_symbols, self.config.max_symbols_per_request):
            rows.extend(self._fetch_batch(batch, start=start, end=reference.astimezone(UTC)))
        return rows

    def _fetch_batch(
        self,
        symbols: list[str],
        *,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]:
        params = {
            "symbols": ",".join(symbols),
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": end.isoformat().replace("+00:00", "Z"),
            "sort": "asc",
            "limit": str(self.config.page_limit),
            "include_content": "false",
        }
        output: list[dict[str, Any]] = []
        page_token: str | None = None
        for _ in range(self.config.max_pages):
            request_params = dict(params)
            if page_token:
                request_params["page_token"] = page_token
            payload = self._get_json(f"{_NEWS_URL}?{urlencode(request_params)}")
            articles = payload.get("news") if isinstance(payload, Mapping) else None
            if not isinstance(articles, list):
                articles = []
            for article in articles:
                if isinstance(article, Mapping):
                    output.extend(article_to_catalyst_rows(article, requested_symbols=symbols))
            token = payload.get("next_page_token") if isinstance(payload, Mapping) else None
            page_token = str(token or "").strip() or None
            if not page_token:
                break
        return output

    def _get_json(self, url: str) -> Mapping[str, Any]:
        if tap_forward.tap_enabled():
            payload = alpaca_sdk._read_via_tap(url)  # noqa: SLF001 - same connector trust boundary
            if not isinstance(payload, Mapping):
                raise RuntimeError("Alpaca news TAP read returned a non-object payload")
            return payload

        cfg = self.alpaca_config
        if not cfg.api_key or not cfg.secret_key:
            raise alpaca_sdk.AlpacaConfigError(
                "Alpaca news access requires configured Alpaca credentials or TAP credential isolation"
            )
        response = self.session.get(
            url,
            headers={
                "APCA-API-KEY-ID": cfg.api_key,
                "APCA-API-SECRET-KEY": cfg.secret_key,
                "Accept": "application/json",
            },
            timeout=cfg.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise RuntimeError("Alpaca news API returned a non-object payload")
        return payload


def article_to_catalyst_rows(
    article: Mapping[str, Any],
    *,
    requested_symbols: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Convert one Alpaca news article into one event row per tagged symbol."""
    published_at = _timestamp(article.get("created_at"))
    if published_at is None:
        return []
    updated_at = _timestamp(article.get("updated_at"))
    headline = str(article.get("headline") or "").strip()
    if not headline:
        return []
    tagged = _symbols(article.get("symbols") if isinstance(article.get("symbols"), list) else [])
    wanted = set(_symbols(requested_symbols))
    symbols = [symbol for symbol in tagged if not wanted or symbol in wanted]
    if not symbols:
        return []

    classification = classify_headline(headline)
    external_id = str(article.get("id") or "").strip() or f"{published_at.isoformat()}:{headline[:200]}"
    metadata = {
        "author": article.get("author"),
        "summary": article.get("summary"),
        "article_symbols": tagged,
        "classifier": "conservative_headline_rules_v1",
    }
    return [
        {
            "external_id": external_id,
            "symbol": symbol,
            "event_type": classification["event_type"],
            "published_at": published_at,
            "updated_at": updated_at,
            "direction": classification["direction"],
            "magnitude": classification["magnitude"],
            "source_quality": 0.78,
            "novelty": 0.80,
            "volatility_risk": classification["volatility_risk"],
            "headline": headline,
            "source": "alpaca_news",
            "url": str(article.get("url") or "").strip() or None,
            "metadata": metadata,
        }
        for symbol in symbols
    ]


def classify_headline(headline: str) -> dict[str, Any]:
    """High-precision, fail-neutral headline classification.

    The rules intentionally leave most headlines neutral. They are an ingestion
    baseline, not a semantic substitute for later corroborated/LLM analysis.
    """
    text = " ".join(str(headline or "").lower().split())
    event_type = "news"
    volatility_risk = 0.10
    magnitude = 0.45

    if _contains(text, r"\b(earnings|eps|quarterly results|revenue)\b"):
        event_type, volatility_risk, magnitude = "earnings", 0.55, 0.72
    elif _contains(text, r"\b(guidance|outlook|forecast)\b"):
        event_type, volatility_risk, magnitude = "guidance", 0.45, 0.72
    elif _contains(text, r"\b(acquire|acquisition|merger|buyout|takeover)\b"):
        event_type, volatility_risk, magnitude = "m_and_a", 0.55, 0.76
    elif _contains(text, r"\b(fda|sec |doj|ftc|regulator|regulatory|approval|approve[sd]?|rejected)\b"):
        event_type, volatility_risk, magnitude = "regulatory", 0.50, 0.70
    elif _contains(text, r"\b(upgrade|downgrade|price target|analyst)\b"):
        event_type, volatility_risk, magnitude = "analyst", 0.18, 0.55
    elif _contains(text, r"\b(contract|award|awarded|purchase order|partnership)\b"):
        event_type, volatility_risk, magnitude = "contract", 0.25, 0.62
    elif _contains(text, r"\b(launch|unveil|release|product|platform)\b"):
        event_type, volatility_risk, magnitude = "product", 0.22, 0.55

    bullish = _contains(
        text,
        r"\b(beats?|raises? guidance|raises? outlook|upgrade[sd]?|approve[sd]?|approval|wins?|awarded|record revenue|record profit|buyback)\b",
    )
    bearish = _contains(
        text,
        r"\b(misses?|cuts? guidance|lowers? outlook|downgrade[sd]?|rejected|denied|probe|investigation|recall|bankruptcy|default)\b",
    )
    # Contradictory or ambiguous language fails neutral.
    direction = "bullish" if bullish and not bearish else "bearish" if bearish and not bullish else "neutral"
    if direction == "neutral":
        magnitude = min(magnitude, 0.50)

    return {
        "event_type": event_type,
        "direction": direction,
        "magnitude": magnitude,
        "volatility_risk": volatility_risk,
    }


def _contains(text: str, pattern: str) -> bool:
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def _timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _symbols(values: Iterable[Any]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        symbol = str(value or "").strip().upper()
        if symbol.endswith(".US"):
            symbol = symbol[:-3]
        if symbol and symbol not in seen:
            seen.add(symbol)
            output.append(symbol)
    return output


def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index:index + size]


__all__ = [
    "AlpacaNewsConfig",
    "AlpacaNewsReader",
    "article_to_catalyst_rows",
    "classify_headline",
]
