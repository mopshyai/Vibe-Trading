from __future__ import annotations

from datetime import datetime, timezone

from src.options_market.alpaca_surface import fetch_alpaca_volatility_surface

UTC = timezone.utc
NOW = datetime(2026, 8, 24, 15, 0, tzinfo=UTC)


class _Config:
    option_feed = "opra"


class FakeReader:
    config = _Config()

    def _contracts(self, underlying, option_type, today):
        assert underlying == "XYZ"
        rows = []
        if option_type == "call":
            rows.extend(
                [
                    {"symbol": "XYZ260918C00100000", "type": "call", "strike_price": "100", "expiration_date": "2026-09-18", "open_interest": "1000", "tradable": True},
                    {"symbol": "XYZ260918C00110000", "type": "call", "strike_price": "110", "expiration_date": "2026-09-18", "open_interest": "500", "tradable": True},
                ]
            )
        else:
            rows.extend(
                [
                    {"symbol": "XYZ260918P00100000", "type": "put", "strike_price": "100", "expiration_date": "2026-09-18", "open_interest": "900", "tradable": True},
                    {"symbol": "XYZ260918P00090000", "type": "put", "strike_price": "90", "expiration_date": "2026-09-18", "open_interest": "450", "tradable": True},
                ]
            )
        return rows

    def _snapshots(self, symbols):
        values = {}
        for symbol in symbols:
            if "C00100000" in symbol:
                values[symbol] = {"latestQuote": {"bp": 5.0, "ap": 5.2}, "impliedVolatility": 0.50, "greeks": {"delta": 0.52}}
            elif "C00110000" in symbol:
                values[symbol] = {"latestQuote": {"bp": 1.7, "ap": 1.9}, "impliedVolatility": 0.48, "greeks": {"delta": 0.25}}
            elif "P00100000" in symbol:
                values[symbol] = {"latestQuote": {"bp": 4.6, "ap": 4.8}, "impliedVolatility": 0.52, "greeks": {"delta": -0.48}}
            else:
                values[symbol] = {"latestQuote": {"bp": 1.5, "ap": 1.7}, "impliedVolatility": 0.57, "greeks": {"delta": -0.25}}
        return values

    def _underlying_mid(self, symbol):
        assert symbol == "XYZ"
        return 100.0


def test_alpaca_surface_adapter_is_read_only_normalization_layer() -> None:
    report = fetch_alpaca_volatility_surface(
        FakeReader(),
        "XYZ",
        now=NOW,
        realized_vol_pct=30.0,
    )
    assert report["status"] == "ok"
    assert report["underlying"] == "XYZ"
    assert report["feed"] == "opra"
    assert report["execution_grade_feed"] is True
    assert report["contracts_fetched"] == 4
    assert report["surface_rows"] == 4
    assert report["expirations"][0]["atm_iv_pct"] == 51.0
    assert report["expirations"][0]["put_call_25d_skew_vol_points"] == 9.0


def test_alpaca_surface_adapter_fails_closed_without_spot() -> None:
    reader = FakeReader()
    reader._underlying_mid = lambda symbol: None
    report = fetch_alpaca_volatility_surface(reader, "XYZ", now=NOW)
    assert report["status"] == "insufficient"
    assert "underlying_quote_unavailable" in report["warnings"]
