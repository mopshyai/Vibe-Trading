from __future__ import annotations

from src.trading.connectors.alpaca.sdk import AlpacaConfig
from src.trading_platform.runtime_config import load_alpaca_runtime_config, runtime_alpaca_status


def test_environment_overlays_saved_config_without_serializing_secrets() -> None:
    base = AlpacaConfig(api_key="old", secret_key="old-secret", profile="live-readonly", feed="sip")
    config = load_alpaca_runtime_config(
        base=base,
        environ={
            "APCA_API_KEY_ID": "paper-key",
            "APCA_API_SECRET_KEY": "paper-secret",
            "ALPACA_PROFILE": "paper",
            "ALPACA_FEED": "iex",
        },
    )
    assert config.api_key == "paper-key"
    assert config.secret_key == "paper-secret"
    assert config.profile == "paper"
    assert config.is_paper is True
    assert config.feed == "iex"
    status = runtime_alpaca_status(config)
    assert status["credentials_configured"] is True
    rendered = str(status)
    assert "paper-key" not in rendered
    assert "paper-secret" not in rendered


def test_alpaca_conventional_names_take_priority() -> None:
    config = load_alpaca_runtime_config(
        base=AlpacaConfig(profile="paper"),
        environ={
            "APCA_API_KEY_ID": "preferred",
            "ALPACA_API_KEY": "fallback",
            "APCA_API_SECRET_KEY": "preferred-secret",
            "ALPACA_SECRET_KEY": "fallback-secret",
        },
    )
    assert config.api_key == "preferred"
    assert config.secret_key == "preferred-secret"
