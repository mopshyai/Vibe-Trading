"""Point-in-time catalyst scoring for U.S. options research.

The scorer is deliberately provider-independent.  Upstream ingestion is
responsible for attaching only information that was actually available at the
research timestamp.  Future-published events are ignored here as a second
anti-lookahead guard.

Scores are ranking features, not probabilities or trade instructions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import math
from typing import Any, Iterable, Mapping


_EVENT_WEIGHTS = {
    "earnings": 1.00,
    "guidance": 0.95,
    "regulatory": 0.95,
    "m_and_a": 0.90,
    "product": 0.80,
    "contract": 0.80,
    "sec_filing": 0.75,
    "macro": 0.65,
    "analyst": 0.55,
    "news": 0.50,
    "scheduled_event": 0.35,
}

_HALF_LIFE_HOURS = {
    "earnings": 72.0,
    "guidance": 96.0,
    "regulatory": 120.0,
    "m_and_a": 120.0,
    "product": 96.0,
    "contract": 96.0,
    "sec_filing": 96.0,
    "macro": 24.0,
    "analyst": 48.0,
    "news": 36.0,
    "scheduled_event": 72.0,
}

_DIRECTIONS = {"bullish": 1.0, "bearish": -1.0, "neutral": 0.0}


@dataclass(frozen=True)
class CatalystEvent:
    """One point-in-time event known to the research system.

    ``published_at`` is the availability timestamp, not necessarily the time the
    underlying real-world event occurred.  That distinction prevents a backtest
    from learning about a filing/headline before the feed actually delivered it.
    """

    symbol: str
    event_type: str
    published_at: datetime
    direction: str = "neutral"
    magnitude: float = 0.5
    source_quality: float = 0.75
    novelty: float = 0.75
    volatility_risk: float = 0.0
    headline: str | None = None
    source: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CatalystEvent":
        published = _as_datetime(value.get("published_at"))
        if published is None:
            raise ValueError("published_at must be an ISO timestamp or datetime")
        event = cls(
            symbol=str(value.get("symbol") or "").strip().upper(),
            event_type=str(value.get("event_type") or "news").strip().lower(),
            published_at=published,
            direction=str(value.get("direction") or "neutral").strip().lower(),
            magnitude=_bounded(value.get("magnitude"), default=0.5),
            source_quality=_bounded(value.get("source_quality"), default=0.75),
            novelty=_bounded(value.get("novelty"), default=0.75),
            volatility_risk=_bounded(value.get("volatility_risk"), default=0.0),
            headline=_optional_text(value.get("headline")),
            source=_optional_text(value.get("source")),
        )
        event.validate()
        return event

    def validate(self) -> None:
        if not self.symbol:
            raise ValueError("symbol is required")
        if self.direction not in _DIRECTIONS:
            raise ValueError("direction must be bullish, bearish, or neutral")
        if self.published_at.tzinfo is None:
            raise ValueError("published_at must be timezone-aware")
        for name, value in (
            ("magnitude", self.magnitude),
            ("source_quality", self.source_quality),
            ("novelty", self.novelty),
            ("volatility_risk", self.volatility_risk),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")


@dataclass(frozen=True)
class CatalystScoreConfig:
    """Weights and safety gates for catalyst aggregation."""

    max_event_age_hours: float = 240.0
    corroboration_bonus: float = 0.08
    max_corroboration_bonus: float = 0.20
    volatility_penalty_weight: float = 0.30
    minimum_signal_score: float = 8.0

    def validate(self) -> None:
        if self.max_event_age_hours <= 0:
            raise ValueError("max_event_age_hours must be positive")
        if not 0 <= self.corroboration_bonus <= 1:
            raise ValueError("corroboration_bonus must be between 0 and 1")
        if not 0 <= self.max_corroboration_bonus <= 1:
            raise ValueError("max_corroboration_bonus must be between 0 and 1")
        if not 0 <= self.volatility_penalty_weight <= 1:
            raise ValueError("volatility_penalty_weight must be between 0 and 1")
        if not 0 <= self.minimum_signal_score <= 100:
            raise ValueError("minimum_signal_score must be between 0 and 100")


def score_catalysts(
    events: Iterable[CatalystEvent | Mapping[str, Any]],
    *,
    as_of: datetime,
    config: CatalystScoreConfig | None = None,
) -> dict[str, dict[str, Any]]:
    """Aggregate point-in-time events into directional catalyst features.

    The returned ``bullish_score`` / ``bearish_score`` are 0-100 ranking
    features.  ``directional_score`` is the score for the dominant direction and
    is suitable for injecting into the existing options ranking pipeline only
    when that direction agrees with the chart/option side.
    """
    cfg = config or CatalystScoreConfig()
    cfg.validate()
    as_of = _require_aware(as_of, "as_of")

    grouped: dict[str, list[dict[str, Any]]] = {}
    for raw in events:
        event = raw if isinstance(raw, CatalystEvent) else CatalystEvent.from_mapping(raw)
        event.validate()
        age_hours = (as_of - event.published_at.astimezone(timezone.utc)).total_seconds() / 3600.0
        if age_hours < 0:
            # Explicit anti-lookahead guard.
            continue
        if age_hours > cfg.max_event_age_hours:
            continue

        event_type = event.event_type if event.event_type in _EVENT_WEIGHTS else "news"
        half_life = _HALF_LIFE_HOURS[event_type]
        decay = math.exp(-math.log(2.0) * age_hours / half_life)
        base = (
            _EVENT_WEIGHTS[event_type]
            * event.magnitude
            * event.source_quality
            * event.novelty
            * decay
        )
        penalty = base * event.volatility_risk * cfg.volatility_penalty_weight
        effective = max(0.0, base - penalty)
        grouped.setdefault(event.symbol, []).append(
            {
                "event": event,
                "age_hours": age_hours,
                "base": base,
                "effective": effective,
                "signed": effective * _DIRECTIONS[event.direction],
            }
        )

    result: dict[str, dict[str, Any]] = {}
    for symbol, rows in grouped.items():
        bullish_rows = [row for row in rows if row["signed"] > 0]
        bearish_rows = [row for row in rows if row["signed"] < 0]
        neutral_rows = [row for row in rows if row["signed"] == 0]

        bullish = _aggregate_side(bullish_rows, cfg)
        bearish = _aggregate_side(bearish_rows, cfg)
        neutral_risk = sum(
            row["event"].volatility_risk * row["effective"] for row in neutral_rows
        )
        direction = "bullish" if bullish > bearish else "bearish" if bearish > bullish else "neutral"
        directional = max(bullish, bearish)
        if directional < cfg.minimum_signal_score:
            direction = "neutral"

        result[symbol] = {
            "symbol": symbol,
            "as_of": as_of.isoformat(),
            "direction": direction,
            "directional_score": round(directional, 2),
            "bullish_score": round(bullish, 2),
            "bearish_score": round(bearish, 2),
            "net_score": round(bullish - bearish, 2),
            "neutral_volatility_risk": round(min(100.0, neutral_risk * 100.0), 2),
            "event_count": len(rows),
            "freshest_event_age_hours": round(min(row["age_hours"] for row in rows), 2),
            "events": [
                {
                    **asdict(row["event"]),
                    "published_at": row["event"].published_at.isoformat(),
                    "age_hours": round(row["age_hours"], 2),
                    "effective_weight": round(row["effective"], 6),
                }
                for row in sorted(rows, key=lambda item: item["age_hours"])
            ],
            "score_interpretation": "ranking feature only; not probability, expected return, or guarantee",
        }
    return result


def directional_catalyst_scores(
    scored: Mapping[str, Mapping[str, Any]],
    chart_candidates: Iterable[Mapping[str, Any]],
) -> dict[str, float]:
    """Return only catalyst scores that agree with each chart candidate side."""
    aligned: dict[str, float] = {}
    for candidate in chart_candidates:
        symbol = str(candidate.get("symbol") or "").strip().upper()
        direction = str(candidate.get("direction") or "").strip().lower()
        context = scored.get(symbol)
        if not context or direction not in ("bullish", "bearish"):
            continue
        if context.get("direction") != direction:
            continue
        field = "bullish_score" if direction == "bullish" else "bearish_score"
        aligned[symbol] = float(context.get(field) or 0.0)
    return aligned


def _aggregate_side(rows: list[dict[str, Any]], cfg: CatalystScoreConfig) -> float:
    if not rows:
        return 0.0
    raw = sum(row["effective"] for row in rows)
    independent_sources = {
        str(row["event"].source or "").strip().lower()
        for row in rows
        if row["event"].source
    }
    corroborating = max(0, len(independent_sources) - 1)
    bonus = min(cfg.max_corroboration_bonus, corroborating * cfg.corroboration_bonus)
    # Saturating transform: a few strong independent events can approach 100,
    # but endlessly duplicated headlines cannot increase the score linearly.
    strength = 1.0 - math.exp(-raw * (1.0 + bonus) * 2.2)
    return min(100.0, max(0.0, strength * 100.0))


def _bounded(value: object, *, default: float) -> float:
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("event numeric fields must be finite numbers") from None
    if not math.isfinite(number):
        raise ValueError("event numeric fields must be finite numbers")
    return number


def _as_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


def _require_aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _optional_text(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


__all__ = [
    "CatalystEvent",
    "CatalystScoreConfig",
    "directional_catalyst_scores",
    "score_catalysts",
]
