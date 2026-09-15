"""Read-only US options-chain research backed by the shared Yahoo client.

``chain`` mode preserves the normalized calls/puts ladder. ``opportunities``
mode ranks defined-risk long calls and puts against a configurable modeled
profit target (default +300%, i.e. a 4x option-premium target). All HTTP routes
through :func:`backtest.loaders.yahoo_client.get_options`.

The opportunity ranking is research-only: it does not place orders and its score
is not a probability, expected return, or guarantee.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from backtest.loaders import yahoo_client
from src.agent.tools import BaseTool
from src.tools._options_opportunity import (
    scan_opportunities,
    validate_explicit_expiration,
)

# Upper bound on contracts emitted per side so a deep chain cannot blow up the
# tool payload handed back to the model.
_MAX_CONTRACTS_PER_SIDE = 60

# Yahoo contract fields we surface, mapped to our snake_case envelope keys.
_CONTRACT_FIELDS = (
    ("contractSymbol", "contract_symbol"),
    ("strike", "strike"),
    ("lastPrice", "last_price"),
    ("bid", "bid"),
    ("ask", "ask"),
    ("volume", "volume"),
    ("openInterest", "open_interest"),
    ("impliedVolatility", "implied_volatility"),
    ("inTheMoney", "in_the_money"),
    ("expiration", "expiration"),
)


class OptionsChainTool(BaseTool):
    """Fetch a US equity option chain or rank asymmetric option candidates."""

    name = "get_options_chain"
    description = (
        "Read-only US options research via Yahoo Finance. mode='chain' (default) "
        "returns calls/puts for one expiration. mode='opportunities' scans liquid "
        "long calls/puts across nearby expirations and ranks them against a "
        "configurable modeled profit target; the default +300% target means the "
        "option premium must reach 4x entry. Ranking is a scenario screen, not a "
        "probability estimate, trade instruction, or guaranteed return. "
        'Example: get_options_chain(ticker="AAPL", mode="opportunities").'
    )
    parameters = {
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": (
                    "US underlying symbol, e.g. 'AAPL' or 'AAPL.US' (the .US "
                    "suffix is stripped by the Yahoo client). Required."
                ),
            },
            "expiration": {
                "type": "integer",
                "description": (
                    "Optional expiration as Unix epoch seconds. In chain mode, "
                    "omit for the nearest expiration. In opportunities mode, "
                    "omit to scan qualifying expirations in the DTE window."
                ),
            },
            "mode": {
                "type": "string",
                "enum": ["chain", "opportunities"],
                "default": "chain",
                "description": (
                    "chain returns the normalized option chain; opportunities "
                    "ranks defined-risk long calls/puts for asymmetric upside."
                ),
            },
            "target_profit_pct": {
                "type": "number",
                "default": 300,
                "description": (
                    "Opportunities mode only. Modeled profit target; 300 means "
                    "+300% profit and therefore a 4x target option premium."
                ),
            },
            "min_dte": {
                "type": "integer",
                "default": 7,
                "description": (
                    "Opportunities mode only. Minimum days to expiration. "
                    "0DTE is intentionally excluded by default."
                ),
            },
            "max_dte": {
                "type": "integer",
                "default": 60,
                "description": "Opportunities mode only. Maximum days to expiration.",
            },
            "max_expirations": {
                "type": "integer",
                "default": 3,
                "description": (
                    "Opportunities mode only. Maximum qualifying expirations "
                    "to scan, capped at 6."
                ),
            },
            "min_open_interest": {
                "type": "integer",
                "default": 100,
                "description": "Opportunities mode only. Minimum open interest.",
            },
            "min_volume": {
                "type": "integer",
                "default": 20,
                "description": "Opportunities mode only. Minimum contract volume.",
            },
            "max_spread_pct": {
                "type": "number",
                "default": 20,
                "description": (
                    "Opportunities mode only. Maximum bid/ask spread as a "
                    "percentage of midpoint."
                ),
            },
            "max_contract_cost_usd": {
                "type": "number",
                "default": 1000,
                "description": (
                    "Opportunities mode only. Maximum debit for one 100-share "
                    "long option contract."
                ),
            },
            "max_results": {
                "type": "integer",
                "default": 10,
                "description": (
                    "Opportunities mode only. Maximum ranked candidates "
                    "returned, capped at 25."
                ),
            },
            "include_itm": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Opportunities mode only. Include in-the-money contracts; "
                    "false by default."
                ),
            },
        },
        "required": ["ticker"],
    }

    def execute(self, **kwargs: Any) -> str:
        """Return a chain or a ranked-opportunity JSON envelope."""
        ticker = str(kwargs.get("ticker") or "").strip()
        if not ticker:
            return _error("ticker is required")

        mode = str(kwargs.get("mode") or "chain").strip().lower()
        if mode not in {"chain", "opportunities"}:
            return _error("mode must be 'chain' or 'opportunities'")

        expiration = kwargs.get("expiration")
        normalized_expiration = _coerce_expiration(expiration)
        if expiration is not None and normalized_expiration is None:
            return _error("expiration must be Unix epoch seconds (integer)")

        if mode == "opportunities":
            return scan_opportunities(ticker, normalized_expiration, kwargs)

        try:
            result = yahoo_client.get_options(
                ticker,
                expiration=normalized_expiration,
            )
        except Exception as exc:  # noqa: BLE001 - surface as error envelope
            return _error(f"yahoo options request failed: {exc}")

        validation_error = validate_explicit_expiration(
            result,
            normalized_expiration,
        )
        if validation_error is not None:
            return _error(validation_error)

        return _success(ticker, result)


def _coerce_expiration(value: Any) -> Optional[int]:
    """Coerce an epoch-second expiration to ``int``; ``None`` when absent/bad."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _success(ticker: str, result: Dict[str, Any]) -> str:
    """Build the success envelope from a quote-chain result mapping."""
    expirations = [
        epoch
        for epoch in (result.get("expirationDates") or [])
        if epoch is not None
    ]
    options = result.get("options") or []
    block = options[0] if options and isinstance(options[0], dict) else {}

    calls = _contracts(block.get("calls"))
    puts = _contracts(block.get("puts"))

    data = {
        "ticker": ticker,
        "expiration": block.get("expirationDate"),
        "expirations": expirations,
        "calls_count": len(calls),
        "puts_count": len(puts),
        "calls": calls,
        "puts": puts,
    }
    return json.dumps(
        {
            "ok": True,
            "market": "us",
            "source": "yahoo",
            "data": data,
        },
        ensure_ascii=False,
    )


def _contracts(raw: Any) -> List[Dict[str, Any]]:
    """Normalize a Yahoo calls/puts array into capped snake_case rows."""
    if not isinstance(raw, list):
        return []
    rows: List[Dict[str, Any]] = []
    for entry in raw[:_MAX_CONTRACTS_PER_SIDE]:
        if not isinstance(entry, dict):
            continue
        rows.append(
            {
                our_key: entry.get(yahoo_key)
                for yahoo_key, our_key in _CONTRACT_FIELDS
            }
        )
    return rows


def _error(message: str) -> str:
    """Render a failure envelope as a JSON string."""
    return json.dumps(
        {"ok": False, "error": message},
        ensure_ascii=False,
    )
