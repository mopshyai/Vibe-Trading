from __future__ import annotations

from datetime import datetime, timezone

from src.options_market.alpaca_equity_bulk import AlpacaBulkEquityReader
from src.trading.connectors.alpaca.sdk import AlpacaConfig

UTC = timezone.utc


class _Response:
    def __init__(self, payload) -> None:  # noqa: ANN001
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):  # noqa: ANN201
        return self.payload


class _Session:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def get(self, url, *, params, headers, timeout):  # noqa: ANN001
        self.calls.append({"url": url, "params": dict(params), "headers": dict(headers), "timeout": timeout})
        token = params.get("page_token")
        if not token:
            return _Response(
                {
                    "bars": {
                        "AAPL": [{"t": "2026-08-20T04:00:00Z", "o": 225, "h": 228, "l": 224, "c": 227, "v": 50_000_000}],
                        "TSLA": [{"t": "2026-08-20T04:00:00Z", "o": 350, "h": 360, "l": 347, "c": 358, "v": 90_000_000}],
                    },
                    "next_page_token": "next",
                }
            )
        return _Response(
            {
                "bars": {
                    "AAPL": [{"t": "2026-08-21T04:00:00Z", "o": 227, "h": 231, "l": 226, "c": 230, "v": 55_000_000}],
                    "TSLA": [{"t": "2026-08-21T04:00:00Z", "o": 358, "h": 365, "l": 355, "c": 362.86, "v": 58_500_000}],
                },
                "next_page_token": None,
            }
        )


def test_bulk_daily_reader_handles_multi_symbol_pagination(monkeypatch) -> None:
    from src.options_market import alpaca_equity_bulk as module

    monkeypatch.setattr(module.tap_forward, "tap_enabled", lambda: False)
    session = _Session()
    reader = AlpacaBulkEquityReader(
        alpaca_config=AlpacaConfig(api_key="paper-key", secret_key="paper-secret", profile="paper"),
        session=session,
        symbol_batch_size=200,
    )
    observed = datetime(2026, 8, 22, 20, 0, tzinfo=UTC)
    frame = reader.fetch_daily_bars(
        ["AAPL", "TSLA.US", "AAPL"],
        start="2026-08-19T00:00:00Z",
        end="2026-08-22T00:00:00Z",
        feed="sip",
        observed_at=observed,
    )
    assert len(frame) == 4
    assert frame["symbol"].tolist() == ["AAPL", "AAPL", "TSLA", "TSLA"]
    assert set(frame["source"]) == {"alpaca:sip:1D:split"}
    assert set(frame["interval"]) == {"1D"}
    assert all(value == observed for value in frame["available_at"])
    assert len(session.calls) == 2
    assert session.calls[0]["params"]["symbols"] == "AAPL,TSLA"
    assert session.calls[0]["params"]["timeframe"] == "1Day"
    assert session.calls[0]["params"]["feed"] == "sip"
    assert session.calls[0]["params"]["adjustment"] == "split"
    assert session.calls[1]["params"]["page_token"] == "next"


def test_bulk_reader_never_calls_network_without_symbols(monkeypatch) -> None:
    from src.options_market import alpaca_equity_bulk as module

    monkeypatch.setattr(module.tap_forward, "tap_enabled", lambda: False)
    session = _Session()
    reader = AlpacaBulkEquityReader(
        alpaca_config=AlpacaConfig(api_key="paper-key", secret_key="paper-secret", profile="paper"),
        session=session,
    )
    frame = reader.fetch_daily_bars(
        [],
        start="2026-08-19T00:00:00Z",
        end="2026-08-22T00:00:00Z",
    )
    assert frame.empty
    assert session.calls == []
