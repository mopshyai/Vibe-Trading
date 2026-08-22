"""Read-only current Alpaca option-chain adapter for focused personal research.

The adapter only performs GET requests. It retrieves active contract metadata
(open interest, expiration, strike) and current option snapshots (bid/ask,
implied volatility and Greeks), then produces bounded long-call/long-put research
candidates. It never places, replaces, cancels or queues an order.

Alpaca's free ``indicative`` option feed is supported explicitly. Callers should
use ``opra`` only when the account is entitled to the official OPRA feed; the
feed used is always surfaced in the result so execution-grade checks can reject
indicative data rather than silently treating it as OPRA.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
import math
from typing import Any, Iterable, Mapping
from urllib.parse import urlencode

import requests

from src.trading import tap_forward
from src.trading.connectors.alpaca import sdk as alpaca_sdk


@dataclass(frozen=True)
class AlpacaCurrentOptionsConfig:
    min_dte: int = 7
    max_dte: int = 60
    option_feed: str = "indicative"
    min_open_interest: int = 100
    max_spread_pct: float = 15.0
    max_contract_cost_usd: float = 1_000.0
    max_required_move_vs_one_sigma: float = 1.75
    max_results: int = 10
    request_timeout_seconds: float = 20.0

    def validate(self) -> None:
        if self.min_dte < 1 or self.max_dte < self.min_dte or self.max_dte > 365:
            raise ValueError("invalid DTE range")
        if self.option_feed not in {"opra", "indicative"}:
            raise ValueError("option_feed must be 'opra' or 'indicative'")
        if self.min_open_interest < 0:
            raise ValueError("min_open_interest cannot be negative")
        if not 0 < self.max_spread_pct <= 100:
            raise ValueError("max_spread_pct must be > 0 and <= 100")
        if self.max_contract_cost_usd <= 0:
            raise ValueError("max_contract_cost_usd must be positive")
        if self.max_required_move_vs_one_sigma <= 0:
            raise ValueError("max_required_move_vs_one_sigma must be positive")
        if not 1 <= self.max_results <= 50:
            raise ValueError("max_results must be between 1 and 50")
        if self.request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")


class AlpacaCurrentOptionsReader:
    """Focused, read-only current-options reader using existing Alpaca config."""

    def __init__(
        self,
        *,
        alpaca_config: alpaca_sdk.AlpacaConfig | None = None,
        config: AlpacaCurrentOptionsConfig | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.alpaca_config = alpaca_config or alpaca_sdk.load_config()
        self.config = config or AlpacaCurrentOptionsConfig()
        self.config.validate()
        self._session = session or requests.Session()

    def fetch_candidates(
        self,
        underlying: str,
        *,
        direction: str,
        target_profit_pct: float = 300.0,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Fetch and rank current long-option candidates for one underlying."""
        clean = str(underlying or "").strip().upper().removesuffix(".US")
        direction = str(direction or "").strip().lower()
        if not clean:
            raise ValueError("underlying is required")
        if direction not in {"bullish", "bearish"}:
            raise ValueError("direction must be bullish or bearish")
        if not 0 < float(target_profit_pct) <= 10_000:
            raise ValueError("target_profit_pct must be > 0 and <= 10000")

        reference = now or datetime.now(timezone.utc)
        if reference.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        option_type = "call" if direction == "bullish" else "put"
        contracts = self._contracts(clean, option_type, reference.date())
        if not contracts:
            return self._empty(clean, direction, target_profit_pct, "no_active_contracts_in_dte_window")

        snapshots = self._snapshots([str(row.get("symbol") or "") for row in contracts])
        spot = self._underlying_mid(clean)
        if spot is None:
            return self._empty(clean, direction, target_profit_pct, "underlying_quote_unavailable")

        candidates: list[dict[str, Any]] = []
        for contract in contracts:
            symbol = str(contract.get("symbol") or "").strip().upper()
            snapshot = snapshots.get(symbol)
            if not symbol or not isinstance(snapshot, Mapping):
                continue
            candidate = _rank_contract(
                underlying=clean,
                contract=contract,
                snapshot=snapshot,
                spot=spot,
                option_type=option_type,
                target_profit_pct=float(target_profit_pct),
                now=reference,
                config=self.config,
            )
            if candidate is not None:
                candidates.append(candidate)

        candidates.sort(
            key=lambda row: (
                -float(row["score"]),
                float(row["required_move_vs_one_sigma"]),
                float(row["spread_pct"]),
                -int(row["open_interest"]),
            )
        )
        selected = candidates[: self.config.max_results]
        for rank, row in enumerate(selected, 1):
            row["rank"] = rank

        return {
            "mode": "alpaca_current_options_focus",
            "underlying": clean,
            "direction": direction,
            "option_type": option_type,
            "spot": round(spot, 4),
            "target_profit_pct": float(target_profit_pct),
            "target_multiple": 1.0 + float(target_profit_pct) / 100.0,
            "feed": self.config.option_feed,
            "execution_grade_feed": self.config.option_feed == "opra",
            "contracts_fetched": len(contracts),
            "snapshots_fetched": len(snapshots),
            "candidate_count": len(selected),
            "candidates": selected,
            "config": asdict(self.config),
            "warning": (
                "Read-only research data. The indicative feed is not OPRA and should not be treated as execution-grade. "
                "Scores are heuristics, not probabilities or return forecasts."
            ),
        }

    def _contracts(self, underlying: str, option_type: str, today: date) -> list[dict[str, Any]]:
        start = today + timedelta(days=self.config.min_dte)
        end = today + timedelta(days=self.config.max_dte)
        params: dict[str, object] = {
            "underlying_symbols": underlying,
            "status": "active",
            "type": option_type,
            "expiration_date_gte": start.isoformat(),
            "expiration_date_lte": end.isoformat(),
            "limit": 10_000,
        }
        rows: list[dict[str, Any]] = []
        page_token: str | None = None
        for _ in range(10):
            if page_token:
                params["page_token"] = page_token
            payload = self._get_json(f"{self.alpaca_config.host}/v2/options/contracts", params)
            values = payload.get("option_contracts") if isinstance(payload, Mapping) else None
            if isinstance(values, list):
                rows.extend(dict(row) for row in values if isinstance(row, Mapping))
            page_token = str(payload.get("next_page_token") or payload.get("page_token") or "").strip() if isinstance(payload, Mapping) else ""
            if not page_token:
                break
        return rows

    def _snapshots(self, symbols: Iterable[str]) -> dict[str, dict[str, Any]]:
        clean = [str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()]
        output: dict[str, dict[str, Any]] = {}
        for index in range(0, len(clean), 100):
            batch = clean[index:index + 100]
            payload = self._get_json(
                f"{alpaca_sdk.DATA_HOST}/v1beta1/options/snapshots",
                {"symbols": ",".join(batch), "feed": self.config.option_feed, "limit": 100},
            )
            snapshots = payload.get("snapshots") if isinstance(payload, Mapping) else None
            if isinstance(snapshots, Mapping):
                for symbol, row in snapshots.items():
                    if isinstance(row, Mapping):
                        output[str(symbol).upper()] = dict(row)
        return output

    def _underlying_mid(self, symbol: str) -> float | None:
        payload = alpaca_sdk.get_quote(symbol, config=self.alpaca_config)
        quote = payload.get("quote") if isinstance(payload, Mapping) else None
        if not isinstance(quote, Mapping):
            return None
        bid = _nonnegative(quote.get("bid") or quote.get("bid_price"))
        ask = _positive(quote.get("ask") or quote.get("ask_price"))
        if bid is not None and ask is not None and ask >= bid and (bid + ask) > 0:
            return (bid + ask) / 2.0
        return ask or bid

    def _get_json(self, url: str, params: Mapping[str, object]) -> Mapping[str, Any]:
        filtered = {key: value for key, value in params.items() if value not in (None, "")}
        if tap_forward.tap_enabled():
            target = f"{url}?{urlencode(filtered)}" if filtered else url
            payload = alpaca_sdk._read_via_tap(target)  # noqa: SLF001 - reuse connector's credential-isolated GET path
            if not isinstance(payload, Mapping):
                raise RuntimeError("Alpaca/TAP returned a non-object response")
            return payload

        if not self.alpaca_config.api_key or not self.alpaca_config.secret_key:
            raise alpaca_sdk.AlpacaConfigError("Alpaca read requires configured api_key and secret_key")
        response = self._session.get(
            url,
            params=filtered,
            headers={
                "APCA-API-KEY-ID": self.alpaca_config.api_key,
                "APCA-API-SECRET-KEY": self.alpaca_config.secret_key,
            },
            timeout=self.config.request_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise RuntimeError("Alpaca returned a non-object response")
        return payload

    def _empty(self, underlying: str, direction: str, target_profit_pct: float, reason: str) -> dict[str, Any]:
        return {
            "mode": "alpaca_current_options_focus",
            "underlying": underlying,
            "direction": direction,
            "target_profit_pct": float(target_profit_pct),
            "feed": self.config.option_feed,
            "execution_grade_feed": self.config.option_feed == "opra",
            "candidate_count": 0,
            "candidates": [],
            "decision": "NO_TRADE",
            "reason": reason,
            "config": asdict(self.config),
        }


def _rank_contract(
    *,
    underlying: str,
    contract: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    spot: float,
    option_type: str,
    target_profit_pct: float,
    now: datetime,
    config: AlpacaCurrentOptionsConfig,
) -> dict[str, Any] | None:
    strike = _positive(contract.get("strike_price") or contract.get("strike"))
    expiration = _date(contract.get("expiration_date"))
    quote = snapshot.get("latestQuote") or snapshot.get("latest_quote") or snapshot.get("quote")
    if not isinstance(quote, Mapping):
        return None
    bid = _nonnegative(quote.get("bp") or quote.get("bid_price") or quote.get("bid"))
    ask = _positive(quote.get("ap") or quote.get("ask_price") or quote.get("ask"))
    iv = _positive(snapshot.get("impliedVolatility") or snapshot.get("implied_volatility"))
    oi = _integer(contract.get("open_interest"), 0)
    tradable = bool(contract.get("tradable", True))
    if strike is None or expiration is None or bid is None or ask is None or ask < bid or iv is None or oi is None:
        return None
    if not tradable or oi < config.min_open_interest:
        return None

    dte = (expiration - now.date()).days
    if not config.min_dte <= dte <= config.max_dte:
        return None
    midpoint = (bid + ask) / 2.0
    if midpoint <= 0:
        return None
    spread_pct = (ask - bid) / midpoint * 100.0
    max_loss = ask * 100.0
    if spread_pct > config.max_spread_pct or max_loss > config.max_contract_cost_usd:
        return None

    multiple = 1.0 + target_profit_pct / 100.0
    target_premium = ask * multiple
    if option_type == "call":
        target_underlying = strike + target_premium
        required_move = max(0.0, (target_underlying - spot) / spot * 100.0)
    else:
        target_underlying = strike - target_premium
        if target_underlying <= 0:
            return None
        required_move = max(0.0, (spot - target_underlying) / spot * 100.0)

    one_sigma = iv * math.sqrt(dte / 365.0) * 100.0
    if one_sigma <= 0:
        return None
    move_ratio = required_move / one_sigma
    if move_ratio > config.max_required_move_vs_one_sigma:
        return None

    spread_score = 30.0 * max(0.0, 1.0 - spread_pct / config.max_spread_pct)
    oi_score = 20.0 * min(1.0, math.log10(oi + 1.0) / 4.0)
    move_score = 40.0 * max(0.0, 1.0 - move_ratio / config.max_required_move_vs_one_sigma)
    dte_score = 10.0 * max(0.0, 1.0 - abs(dte - 30.0) / 30.0)
    score = spread_score + oi_score + move_score + dte_score

    greeks = snapshot.get("greeks") if isinstance(snapshot.get("greeks"), Mapping) else {}
    contract_symbol = str(contract.get("symbol") or "").strip().upper()
    return {
        "symbol": underlying,
        "underlying": underlying,
        "contract_symbol": contract_symbol,
        "direction": "bullish" if option_type == "call" else "bearish",
        "option_type": option_type,
        "strike": round(strike, 4),
        "expiration": expiration.isoformat(),
        "dte": dte,
        "spot": round(spot, 4),
        "bid": round(bid, 4),
        "entry_ask": round(ask, 4),
        "spread_pct": round(spread_pct, 4),
        "open_interest": oi,
        "volume": None,
        "implied_volatility": round(iv, 8),
        "delta": _finite(greeks.get("delta")),
        "gamma": _finite(greeks.get("gamma")),
        "theta": _finite(greeks.get("theta")),
        "vega": _finite(greeks.get("vega")),
        "max_loss_usd": round(max_loss, 2),
        "target_profit_pct": target_profit_pct,
        "target_multiple": round(multiple, 4),
        "target_premium": round(target_premium, 4),
        "target_underlying_at_expiry": round(target_underlying, 4),
        "required_underlying_move_pct": round(required_move, 4),
        "one_sigma_implied_move_pct": round(one_sigma, 4),
        "required_move_vs_one_sigma": round(move_ratio, 4),
        "score": round(score, 2),
        "ranking_score": round(score, 2),
        "data_source": "alpaca",
        "option_feed": config.option_feed,
        "execution_grade_feed": config.option_feed == "opra",
        "data_warnings": ["current_option_volume_not_provided_by_snapshot_endpoint"],
    }


def _finite(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _positive(value: object) -> float | None:
    number = _finite(value)
    return number if number is not None and number > 0 else None


def _nonnegative(value: object) -> float | None:
    number = _finite(value)
    return number if number is not None and number >= 0 else None


def _integer(value: object, default: int | None = None) -> int | None:
    number = _finite(value)
    if number is None or int(number) != number:
        return default
    return int(number)


def _date(value: object) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


__all__ = ["AlpacaCurrentOptionsConfig", "AlpacaCurrentOptionsReader"]
