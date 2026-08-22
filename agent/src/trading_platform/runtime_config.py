"""Ephemeral runtime configuration for hosted Trading Desk processes.

The legacy connector supports a local ``~/.vibe-trading/alpaca.json`` file. A
container should not need to persist broker secrets to disk, so platform workers
can overlay that configuration from environment variables at process startup.
Nothing in this module writes credentials or returns a serialised secret payload.

Supported key pairs (first non-empty value wins):
- ``APCA_API_KEY_ID`` / ``APCA_API_SECRET_KEY`` (Alpaca conventional names)
- ``ALPACA_API_KEY_ID`` / ``ALPACA_SECRET_KEY``
- ``ALPACA_API_KEY`` / ``ALPACA_SECRET_KEY``

Optional non-secret controls:
- ``ALPACA_PROFILE``: paper, live-readonly, or live
- ``ALPACA_FEED``: iex or sip for stock reads
"""

from __future__ import annotations

from dataclasses import asdict
import os
from typing import Mapping

from src.trading.connectors.alpaca import sdk as alpaca_sdk


def load_alpaca_runtime_config(
    *,
    environ: Mapping[str, str] | None = None,
    base: alpaca_sdk.AlpacaConfig | None = None,
) -> alpaca_sdk.AlpacaConfig:
    """Load saved config and overlay process environment without persisting it."""
    env = environ if environ is not None else os.environ
    cfg = base or alpaca_sdk.load_config()
    payload = asdict(cfg)

    api_key = _first(env, "APCA_API_KEY_ID", "ALPACA_API_KEY_ID", "ALPACA_API_KEY")
    secret_key = _first(env, "APCA_API_SECRET_KEY", "ALPACA_SECRET_KEY")
    profile = _first(env, "ALPACA_PROFILE", "VIBE_ALPACA_PROFILE")
    feed = _first(env, "ALPACA_FEED", "ALPACA_STOCK_FEED")

    if api_key:
        payload["api_key"] = api_key
    if secret_key:
        payload["secret_key"] = secret_key
    if profile:
        payload["profile"] = profile.lower()
    if feed:
        payload["feed"] = feed.lower()
    return alpaca_sdk.AlpacaConfig.from_mapping(payload)


def runtime_alpaca_status(config: alpaca_sdk.AlpacaConfig) -> dict[str, object]:
    """Return a secret-free config summary safe for preflight/logging."""
    return {
        "profile": config.profile,
        "is_paper": config.is_paper,
        "host": config.host,
        "stock_feed": config.feed,
        "credentials_configured": bool(config.api_key and config.secret_key),
    }


def _first(env: Mapping[str, str], *names: str) -> str:
    for name in names:
        value = str(env.get(name) or "").strip()
        if value:
            return value
    return ""


__all__ = ["load_alpaca_runtime_config", "runtime_alpaca_status"]
