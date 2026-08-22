from __future__ import annotations

from src.trading.connectors.alpaca.sdk import AlpacaConfig, PAPER_HOST
from src.trading_platform.paper_broker_read import fetch_paper_account_positions


class _Response:
    def __init__(self, payload) -> None:  # noqa: ANN001
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):  # noqa: ANN201
        return self.payload


class _Session:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def get(self, url, *, headers, timeout):  # noqa: ANN001
        self.urls.append(url)
        assert headers["APCA-API-KEY-ID"] == "paper-key"
        if url.endswith("/v2/account"):
            return _Response({"status": "ACTIVE", "equity": "100000", "buying_power": "90000", "trading_blocked": False})
        if url.endswith("/v2/positions"):
            return _Response(
                [
                    {
                        "symbol": "TSLA260918C00400000",
                        "asset_class": "us_option",
                        "qty": "1",
                        "side": "long",
                        "avg_entry_price": "2.72",
                        "cost_basis": "272",
                        "market_value": "300",
                    },
                    {
                        "symbol": "AAPL",
                        "asset_class": "us_equity",
                        "qty": "5",
                        "side": "long",
                        "avg_entry_price": "200",
                        "cost_basis": "1000",
                        "market_value": "1010",
                    },
                ]
            )
        raise AssertionError(f"unexpected URL {url}")


def test_paper_rest_reader_normalizes_asset_classes(monkeypatch) -> None:
    from src.trading_platform import paper_broker_read as module

    monkeypatch.setattr(module.tap_forward, "tap_enabled", lambda: False)
    config = AlpacaConfig(api_key="paper-key", secret_key="paper-secret", profile="paper")
    session = _Session()
    result = fetch_paper_account_positions(config, session=session)
    assert result["is_paper"] is True
    assert result["account"]["equity"] == "100000"
    assert result["positions"][0]["asset_class"] == "option"
    assert result["positions"][1]["asset_class"] == "equity"
    assert session.urls == [f"{PAPER_HOST}/v2/account", f"{PAPER_HOST}/v2/positions"]


def test_live_profile_is_rejected_before_network(monkeypatch) -> None:
    from src.trading_platform import paper_broker_read as module

    monkeypatch.setattr(module.tap_forward, "tap_enabled", lambda: False)
    session = _Session()
    live = AlpacaConfig(api_key="live", secret_key="secret", profile="live")
    try:
        fetch_paper_account_positions(live, session=session)
    except RuntimeError as exc:
        assert "paper profile" in str(exc).lower()
    else:
        raise AssertionError("live profile should fail closed")
    assert session.urls == []
