from __future__ import annotations

from datetime import datetime, timezone

from src.options_market.alpaca_news import AlpacaNewsConfig, AlpacaNewsReader
from src.trading.connectors.alpaca.sdk import AlpacaConfig

UTC = timezone.utc


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class _Session:
    def __init__(self) -> None:
        self.urls: list[str] = []
        self.headers: list[dict] = []

    def get(self, url, *, headers, timeout):  # noqa: ANN001
        self.urls.append(str(url))
        self.headers.append(dict(headers))
        assert timeout == 7.0
        return _Response(
            {
                "news": [
                    {
                        "id": 77,
                        "headline": "Analyst Upgrades ABC After Product Launch",
                        "created_at": "2026-08-22T14:30:00Z",
                        "updated_at": "2026-08-22T14:31:00Z",
                        "symbols": ["ABC"],
                        "url": "https://example.test/abc",
                    }
                ],
                "next_page_token": None,
            }
        )


def test_reader_fetches_news_with_read_only_market_data_credentials(monkeypatch) -> None:
    from src.options_market import alpaca_news

    monkeypatch.setattr(alpaca_news.tap_forward, "tap_enabled", lambda: False)
    session = _Session()
    reader = AlpacaNewsReader(
        config=AlpacaNewsConfig(lookback_hours=24, max_pages=2),
        alpaca_config=AlpacaConfig(
            api_key="test-key",
            secret_key="test-secret",
            profile="paper",
            timeout=7.0,
        ),
        session=session,  # type: ignore[arg-type]
    )

    rows = reader.fetch(
        ["ABC.US"],
        now=datetime(2026, 8, 22, 15, 0, tzinfo=UTC),
    )
    assert len(rows) == 1
    assert rows[0]["symbol"] == "ABC"
    assert rows[0]["direction"] == "bullish"
    assert "/v1beta1/news?" in session.urls[0]
    assert "symbols=ABC" in session.urls[0]
    assert session.headers[0]["APCA-API-KEY-ID"] == "test-key"
    assert session.headers[0]["APCA-API-SECRET-KEY"] == "test-secret"
