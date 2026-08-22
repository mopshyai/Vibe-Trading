"""Fail-closed data freshness checks for the Trading Desk."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field

from .models import ComponentHealth, DataQualitySummary, HealthStatus


class FreshnessPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_age_seconds: float = Field(gt=0)
    blocking: bool = True


DEFAULT_FRESHNESS_POLICIES: dict[str, FreshnessPolicy] = {
    "equity_universe": FreshnessPolicy(max_age_seconds=86_400, blocking=True),
    # Focused underlying quotes used for current option repricing.
    "equity_market": FreshnessPolicy(max_age_seconds=120, blocking=True),
    # Whole-market 1D bars used by the cross-sectional chart screen. The worker
    # refreshes these once per market date; 30h allows normal overnight/session
    # boundaries. The refresh CLI separately rejects source bars whose actual
    # event timestamp is more than five calendar days old, covering weekends and
    # exchange holidays without treating ancient data as current.
    "equity_daily_bars": FreshnessPolicy(max_age_seconds=30 * 3600, blocking=True),
    "options_market": FreshnessPolicy(max_age_seconds=60, blocking=True),
    "market_calendar": FreshnessPolicy(max_age_seconds=86_400, blocking=True),
    "catalysts": FreshnessPolicy(max_age_seconds=900, blocking=False),
    "empirical_ev": FreshnessPolicy(max_age_seconds=7 * 86_400, blocking=True),
    "walk_forward": FreshnessPolicy(max_age_seconds=7 * 86_400, blocking=True),
    "account_state": FreshnessPolicy(max_age_seconds=60, blocking=True),
}


def evaluate_data_freshness(
    observations: Mapping[str, Mapping[str, Any]],
    *,
    now: datetime | None = None,
    policies: Mapping[str, FreshnessPolicy] | None = None,
) -> DataQualitySummary:
    """Evaluate required research/account inputs without inventing freshness.

    Each observation may contain ``observed_at``, ``source``, ``detail`` and an
    optional explicit ``error`` string. Missing or invalid timestamps are
    ``unknown``; a present error is ``error``; old data is ``stale``.
    """
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    active = dict(policies or DEFAULT_FRESHNESS_POLICIES)
    components: list[ComponentHealth] = []

    for name, policy in active.items():
        raw = observations.get(name) if isinstance(observations.get(name), Mapping) else {}
        explicit_error = str(raw.get("error") or "").strip()
        observed_at = _timestamp(raw.get("observed_at"))
        age_seconds: float | None = None
        if observed_at is not None:
            age_seconds = max(
                0.0,
                (
                    reference.astimezone(timezone.utc)
                    - observed_at.astimezone(timezone.utc)
                ).total_seconds(),
            )

        if explicit_error:
            status = HealthStatus.ERROR
            detail = explicit_error
        elif observed_at is None:
            status = HealthStatus.UNKNOWN
            detail = str(raw.get("detail") or "missing observed_at")
        elif age_seconds is not None and age_seconds > policy.max_age_seconds:
            status = HealthStatus.STALE
            detail = str(
                raw.get("detail")
                or f"age {age_seconds:.1f}s exceeds {policy.max_age_seconds:.1f}s"
            )
        else:
            status = HealthStatus.OK
            detail = str(raw.get("detail") or "fresh")

        components.append(
            ComponentHealth(
                name=name,
                status=status,
                observed_at=observed_at,
                age_seconds=age_seconds,
                source=str(raw.get("source") or "").strip() or None,
                detail=detail,
                blocking=policy.blocking,
            )
        )

    return DataQualitySummary.from_components(components)


def _timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


__all__ = ["DEFAULT_FRESHNESS_POLICIES", "FreshnessPolicy", "evaluate_data_freshness"]
