"""High-upside options opportunity ranking for :mod:`options_chain_tool`.

This module is deliberately not a ``BaseTool`` subclass (its leading underscore
also keeps it out of tool auto-discovery). It provides the read-only research
logic used by ``get_options_chain(mode="opportunities")`` without increasing
the public tool count.

A configured profit target is a scenario target, not a forecast. For example,
+300% profit means a long option bought at $1.00 would need to be worth $4.00.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from typing import Any, Dict, Optional

from backtest.loaders import yahoo_client


DEFAULT_TARGET_PROFIT_PCT = 300.0
DEFAULT_MIN_DTE = 7
DEFAULT_MAX_DTE = 60
DEFAULT_MAX_EXPIRATIONS = 3
DEFAULT_MIN_OPEN_INTEREST = 100
DEFAULT_MIN_VOLUME = 20
DEFAULT_MAX_SPREAD_PCT = 20.0
DEFAULT_MAX_CONTRACT_COST_USD = 1_000.0
DEFAULT_MAX_RESULTS = 10


@dataclass(frozen=True)
class OpportunityConfig:
    """Validated options-opportunity screening parameters."""

    target_profit_pct: float = DEFAULT_TARGET_PROFIT_PCT
    min_dte: int = DEFAULT_MIN_DTE
    max_dte: int = DEFAULT_MAX_DTE
    max_expirations: int = DEFAULT_MAX_EXPIRATIONS
    min_open_interest: int = DEFAULT_MIN_OPEN_INTEREST
    min_volume: int = DEFAULT_MIN_VOLUME
    max_spread_pct: float = DEFAULT_MAX_SPREAD_PCT
    max_contract_cost_usd: float = DEFAULT_MAX_CONTRACT_COST_USD
    max_results: int = DEFAULT_MAX_RESULTS
    include_itm: bool = False

    @property
    def target_multiple(self) -> float:
        """Option-value multiple required to realize the profit target."""
        return 1.0 + self.target_profit_pct / 100.0

    @classmethod
    def from_kwargs(cls, kwargs: Dict[str, Any]) -> "OpportunityConfig":
        """Build a fail-closed config from tool arguments."""
        config = cls(
            target_profit_pct=_finite_float(
                kwargs.get("target_profit_pct", DEFAULT_TARGET_PROFIT_PCT),
                "target_profit_pct",
            ),
            min_dte=_int_value(kwargs.get("min_dte", DEFAULT_MIN_DTE), "min_dte"),
            max_dte=_int_value(kwargs.get("max_dte", DEFAULT_MAX_DTE), "max_dte"),
            max_expirations=_int_value(
                kwargs.get("max_expirations", DEFAULT_MAX_EXPIRATIONS),
                "max_expirations",
            ),
            min_open_interest=_int_value(
                kwargs.get("min_open_interest", DEFAULT_MIN_OPEN_INTEREST),
                "min_open_interest",
            ),
            min_volume=_int_value(
                kwargs.get("min_volume", DEFAULT_MIN_VOLUME),
                "min_volume",
            ),
            max_spread_pct=_finite_float(
                kwargs.get("max_spread_pct", DEFAULT_MAX_SPREAD_PCT),
                "max_spread_pct",
            ),
            max_contract_cost_usd=_finite_float(
                kwargs.get(
                    "max_contract_cost_usd",
                    DEFAULT_MAX_CONTRACT_COST_USD,
                ),
                "max_contract_cost_usd",
            ),
            max_results=_int_value(
                kwargs.get("max_results", DEFAULT_MAX_RESULTS),
                "max_results",
            ),
            include_itm=bool(kwargs.get("include_itm", False)),
        )
        config.validate()
        return config

    def validate(self) -> None:
        """Reject unsafe or nonsensical scan settings."""
        if not 0 < self.target_profit_pct <= 10_000:
            raise ValueError("target_profit_pct must be > 0 and <= 10000")
        if self.min_dte < 1:
            raise ValueError(
                "min_dte must be at least 1; 0DTE is intentionally excluded"
            )
        if self.max_dte < self.min_dte or self.max_dte > 365:
            raise ValueError("max_dte must be >= min_dte and <= 365")
        if not 1 <= self.max_expirations <= 6:
            raise ValueError("max_expirations must be between 1 and 6")
        if self.min_open_interest < 0 or self.min_volume < 0:
            raise ValueError(
                "min_open_interest and min_volume must be non-negative"
            )
        if not 0 < self.max_spread_pct <= 200:
            raise ValueError("max_spread_pct must be > 0 and <= 200")
        if self.max_contract_cost_usd <= 0:
            raise ValueError("max_contract_cost_usd must be positive")
        if not 1 <= self.max_results <= 25:
            raise ValueError("max_results must be between 1 and 25")

    def public_filters(self) -> Dict[str, Any]:
        """Return settings safe and useful to show in the result envelope."""
        return {
            "min_dte": self.min_dte,
            "max_dte": self.max_dte,
            "max_expirations": self.max_expirations,
            "min_open_interest": self.min_open_interest,
            "min_volume": self.min_volume,
            "max_spread_pct": self.max_spread_pct,
            "max_contract_cost_usd": self.max_contract_cost_usd,
            "include_itm": self.include_itm,
        }


def scan_opportunities(
    ticker: str,
    expiration: Optional[int],
    kwargs: Dict[str, Any],
) -> str:
    """Scan one underlying and return ranked long-call/long-put candidates."""
    try:
        config = OpportunityConfig.from_kwargs(kwargs)
    except ValueError as exc:
        return _error(str(exc))

    now = datetime.now(timezone.utc)
    try:
        first = yahoo_client.get_options(ticker, expiration=expiration)
    except Exception as exc:  # noqa: BLE001 - tool boundary returns an envelope
        return _error(f"yahoo options request failed: {exc}")

    validation_error = validate_explicit_expiration(first, expiration)
    if validation_error is not None:
        return _error(validation_error)

    spot = extract_spot(first)
    if spot is None or spot <= 0:
        return _error(
            "Yahoo options response did not contain a usable underlying spot price"
        )

    available = [
        int(epoch)
        for epoch in (first.get("expirationDates") or [])
        if _integer_or_none(epoch) is not None
    ]
    if expiration is not None:
        selected = [expiration]
    else:
        selected = [
            epoch
            for epoch in available
            if config.min_dte <= days_to_expiry(epoch, now) <= config.max_dte
        ][: config.max_expirations]

    if not selected:
        return _success_payload(
            ticker=ticker,
            spot=spot,
            config=config,
            available=available,
            scanned=[],
            examined=0,
            candidates=[],
            warning=(
                "No expiration matched the configured DTE window. "
                "This scanner ranks scenarios only; it does not predict or "
                "guarantee returns."
            ),
        )

    first_block = first_options_block(first)
    first_expiration = (
        _integer_or_none(first_block.get("expirationDate"))
        if first_block is not None
        else None
    )

    candidates: list[Dict[str, Any]] = []
    examined = 0
    scanned: list[int] = []

    for selected_expiration in selected:
        if selected_expiration == first_expiration:
            result = first
        else:
            try:
                result = yahoo_client.get_options(
                    ticker,
                    expiration=selected_expiration,
                )
            except Exception:  # noqa: BLE001 - a single cycle may fail closed
                continue
            cycle_error = validate_explicit_expiration(
                result,
                selected_expiration,
            )
            if cycle_error is not None:
                continue

        block = first_options_block(result)
        if block is None:
            continue

        dte = days_to_expiry(selected_expiration, now)
        if not config.min_dte <= dte <= config.max_dte:
            continue

        cycle_spot = extract_spot(result) or spot
        scanned.append(selected_expiration)
        for option_type, field in (("call", "calls"), ("put", "puts")):
            raw_contracts = block.get(field)
            if not isinstance(raw_contracts, list):
                continue
            for contract in raw_contracts:
                if not isinstance(contract, dict):
                    continue
                examined += 1
                candidate = rank_contract(
                    ticker=ticker,
                    option_type=option_type,
                    contract=contract,
                    spot=cycle_spot,
                    expiration=selected_expiration,
                    dte=dte,
                    config=config,
                )
                if candidate is not None:
                    candidates.append(candidate)

    candidates.sort(
        key=lambda item: (
            -float(item["score"]),
            float(item["required_move_vs_one_sigma"]),
            float(item["spread_pct"]),
            -int(item["open_interest"]),
        )
    )
    limited = candidates[: config.max_results]
    for rank, candidate in enumerate(limited, start=1):
        candidate["rank"] = rank

    return _success_payload(
        ticker=ticker,
        spot=spot,
        config=config,
        available=available,
        scanned=scanned,
        examined=examined,
        candidates=limited,
        eligible_count=len(candidates),
    )


def rank_contract(
    *,
    ticker: str,
    option_type: str,
    contract: Dict[str, Any],
    spot: float,
    expiration: int,
    dte: int,
    config: OpportunityConfig,
) -> Optional[Dict[str, Any]]:
    """Return one eligible candidate or ``None`` when a filter rejects it."""
    strike = _number(contract.get("strike"))
    bid = _number(contract.get("bid"))
    ask = _number(contract.get("ask"))
    iv = _number(contract.get("impliedVolatility"))
    open_interest = _whole_number(contract.get("openInterest"))
    volume = _whole_number(contract.get("volume"))
    in_the_money = bool(contract.get("inTheMoney", False))

    if (
        strike is None
        or strike <= 0
        or bid is None
        or bid < 0
        or ask is None
        or ask <= 0
        or ask < bid
        or iv is None
        or iv <= 0
    ):
        return None
    if open_interest < config.min_open_interest or volume < config.min_volume:
        return None
    if in_the_money and not config.include_itm:
        return None

    midpoint = (bid + ask) / 2.0
    if midpoint <= 0:
        return None
    spread_pct = (ask - bid) / midpoint * 100.0
    if spread_pct > config.max_spread_pct:
        return None

    max_loss_usd = ask * 100.0
    if max_loss_usd > config.max_contract_cost_usd:
        return None

    target_premium = ask * config.target_multiple
    if option_type == "call":
        target_underlying = strike + target_premium
        breakeven = strike + ask
        required_move_pct = max(
            0.0,
            (target_underlying - spot) / spot * 100.0,
        )
    elif option_type == "put":
        target_underlying = strike - target_premium
        if target_underlying <= 0:
            return None
        breakeven = strike - ask
        required_move_pct = max(
            0.0,
            (spot - target_underlying) / spot * 100.0,
        )
    else:
        return None

    one_sigma_move_pct = iv * math.sqrt(dte / 365.0) * 100.0
    if one_sigma_move_pct <= 0:
        return None
    move_ratio = required_move_pct / one_sigma_move_pct

    spread_score = 25.0 * max(
        0.0,
        1.0 - spread_pct / config.max_spread_pct,
    )
    oi_score = 15.0 * min(
        1.0,
        math.log10(open_interest + 1.0) / 4.0,
    )
    volume_score = 10.0 * min(
        1.0,
        math.log10(volume + 1.0) / 3.0,
    )
    move_score = 40.0 * max(0.0, 1.0 - move_ratio / 3.0)
    dte_score = 10.0 * max(0.0, 1.0 - abs(dte - 30.0) / 30.0)
    score = spread_score + oi_score + volume_score + move_score + dte_score

    return {
        "ticker": ticker,
        "contract_symbol": contract.get("contractSymbol"),
        "option_type": option_type,
        "strike": round(strike, 4),
        "expiration": expiration,
        "dte": dte,
        "spot": round(spot, 4),
        "bid": round(bid, 4),
        "entry_ask": round(ask, 4),
        "spread_pct": round(spread_pct, 2),
        "open_interest": open_interest,
        "volume": volume,
        "implied_volatility": round(iv, 6),
        "in_the_money": in_the_money,
        "max_loss_usd": round(max_loss_usd, 2),
        "target_profit_pct": config.target_profit_pct,
        "target_multiple": round(config.target_multiple, 4),
        "target_premium": round(target_premium, 4),
        "modeled_profit_usd": round((target_premium - ask) * 100.0, 2),
        "breakeven_at_expiry": round(breakeven, 4),
        "target_underlying_at_expiry": round(target_underlying, 4),
        "required_underlying_move_pct": round(required_move_pct, 2),
        "one_sigma_implied_move_pct": round(one_sigma_move_pct, 2),
        "required_move_vs_one_sigma": round(move_ratio, 3),
        "score": round(score, 2),
        "score_components": {
            "spread": round(spread_score, 2),
            "open_interest": round(oi_score, 2),
            "volume": round(volume_score, 2),
            "move_feasibility": round(move_score, 2),
            "dte": round(dte_score, 2),
        },
    }


def validate_explicit_expiration(
    result: Dict[str, Any],
    expiration: Optional[int],
) -> Optional[str]:
    """Ensure an explicitly requested expiration matches Yahoo's response."""
    if expiration is None:
        return None

    dates = result.get("expirationDates")
    if not isinstance(dates, list):
        return "malformed Yahoo response: expirationDates is not a list"
    if expiration not in dates:
        suffix = "..." if len(dates) > 8 else ""
        return (
            f"expiration {expiration} is not among the available dates; "
            f"available: {dates[:8]}{suffix}"
        )

    options = result.get("options") or []
    if not isinstance(options, list) or not options:
        return (
            f"no option chain returned for expiration {expiration} although "
            f"Yahoo lists that date; retry, or pick another expiration from: "
            f"{dates[:8]}"
        )

    block = options[0]
    if not isinstance(block, dict):
        return "malformed Yahoo response: options block is not a dict"
    block_date = block.get("expirationDate")
    if block_date != expiration:
        return (
            f"expiration {expiration} did not match the returned chain "
            f"(block expiration is {block_date})"
        )
    return None


def extract_spot(result: Dict[str, Any]) -> Optional[float]:
    """Return the first usable Yahoo underlying price."""
    quote = result.get("quote")
    if not isinstance(quote, dict):
        return None
    for field in (
        "regularMarketPrice",
        "postMarketPrice",
        "preMarketPrice",
        "regularMarketPreviousClose",
    ):
        value = _number(quote.get(field))
        if value is not None and value > 0:
            return value
    return None


def first_options_block(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the first option block when structurally valid."""
    options = result.get("options") or []
    if (
        not isinstance(options, list)
        or not options
        or not isinstance(options[0], dict)
    ):
        return None
    return options[0]


def days_to_expiry(expiration: int, now: datetime) -> int:
    """Calendar days from ``now`` to an option expiration epoch."""
    expiration_date = datetime.fromtimestamp(expiration, timezone.utc).date()
    return (expiration_date - now.date()).days


def _success_payload(
    *,
    ticker: str,
    spot: float,
    config: OpportunityConfig,
    available: list[int],
    scanned: list[int],
    examined: int,
    candidates: list[Dict[str, Any]],
    eligible_count: Optional[int] = None,
    warning: Optional[str] = None,
) -> str:
    if warning is None:
        warning = (
            "Research screen only. Scores compare liquidity, spread, DTE and "
            "the underlying move required for the modeled premium target against "
            "the option's one-sigma implied move. They are not probabilities, "
            "expected returns, trade instructions, or guarantees."
        )
    payload = {
        "ok": True,
        "market": "us",
        "source": "yahoo",
        "mode": "opportunities",
        "data": {
            "ticker": ticker,
            "spot": round(spot, 4),
            "target_profit_pct": config.target_profit_pct,
            "target_multiple": config.target_multiple,
            "target_definition": (
                f"+{config.target_profit_pct:g}% profit means the option premium "
                f"must reach {config.target_multiple:g}x the screened entry ask"
            ),
            "available_expirations": available,
            "scanned_expirations": scanned,
            "examined_contracts": examined,
            "eligible_count": (
                len(candidates) if eligible_count is None else eligible_count
            ),
            "candidates": candidates,
            "filters": config.public_filters(),
        },
        "warning": warning,
    }
    return json.dumps(payload, ensure_ascii=False, allow_nan=False)


def _number(value: Any) -> Optional[float]:
    if isinstance(value, dict):
        value = value.get("raw")
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _whole_number(value: Any) -> int:
    number = _number(value)
    if number is None or number < 0:
        return 0
    return int(number)


def _finite_float(value: Any, name: str) -> float:
    number = _number(value)
    if number is None:
        raise ValueError(f"{name} must be a finite number")
    return number


def _int_value(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be an integer") from None


def _integer_or_none(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _error(message: str) -> str:
    return json.dumps({"ok": False, "error": message}, ensure_ascii=False)
