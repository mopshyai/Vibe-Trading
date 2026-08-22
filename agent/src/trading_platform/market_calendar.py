"""Authoritative U.S. regular-session calendar adapter.

The adapter uses ``exchange_calendars``' XNYS calendar so holidays and early
closes are data, not hand-written weekday assumptions. The dependency is imported
lazily so unrelated Vibe-Trading workflows remain usable when the trading-platform
calendar extra has not been installed.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from src.options_market.continuous import TradingSession

_ET = ZoneInfo("America/New_York")
_UTC = timezone.utc
_SOURCE = "exchange_calendars:XNYS"


class MarketCalendarDependencyError(RuntimeError):
    """Raised when the optional authoritative calendar dependency is absent."""


def xnys_trading_session(now: datetime) -> TradingSession:
    """Return the authoritative NYSE core session for ``now``'s ET date.

    A non-session day is represented with ``trading_day=False``. The placeholder
    open/close timestamps on a closed day exist only because ``TradingSession``
    requires an ordered pair; callers must obey the trading-day flag.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    session_date = now.astimezone(_ET).date()
    calendar = _calendar(session_date - timedelta(days=7), session_date + timedelta(days=7))
    label = pd.Timestamp(session_date)

    if not bool(calendar.is_session(label)):
        return TradingSession(
            open_at=datetime.combine(session_date, time(9, 30), tzinfo=_ET).astimezone(_UTC),
            close_at=datetime.combine(session_date, time(16, 0), tzinfo=_ET).astimezone(_UTC),
            trading_day=False,
            source=_SOURCE,
            authoritative=True,
        )

    open_at = _to_datetime(calendar.session_open(label))
    close_at = _to_datetime(calendar.session_close(label))
    return TradingSession(
        open_at=open_at,
        close_at=close_at,
        trading_day=True,
        source=_SOURCE,
        authoritative=True,
    )


def xnys_session_calendar(start: object, end: object) -> dict[str, dict[str, Any]]:
    """Build an explicit date-keyed core-session calendar for runtime export."""
    start_date = _date(start, "start")
    end_date = _date(end, "end")
    if end_date < start_date:
        raise ValueError("end must be >= start")
    calendar = _calendar(start_date - timedelta(days=7), end_date + timedelta(days=7))

    result: dict[str, dict[str, Any]] = {}
    current = start_date
    while current <= end_date:
        label = pd.Timestamp(current)
        if bool(calendar.is_session(label)):
            open_at = _to_datetime(calendar.session_open(label))
            close_at = _to_datetime(calendar.session_close(label))
            result[current.isoformat()] = {
                "open": open_at.isoformat(),
                "close": close_at.isoformat(),
                "trading_day": True,
                "early_close": close_at.astimezone(_ET).time() < time(16, 0),
                "source": _SOURCE,
            }
        else:
            result[current.isoformat()] = {
                "open": datetime.combine(current, time(9, 30), tzinfo=_ET).astimezone(_UTC).isoformat(),
                "close": datetime.combine(current, time(16, 0), tzinfo=_ET).astimezone(_UTC).isoformat(),
                "trading_day": False,
                "early_close": False,
                "source": _SOURCE,
            }
        current += timedelta(days=1)
    return result


def _calendar(start: date, end: date):  # type: ignore[no-untyped-def]
    try:
        import exchange_calendars as xcals  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised by deployments missing optional dep
        raise MarketCalendarDependencyError(
            "Authoritative U.S. market sessions require exchange_calendars>=4.13.2,<5"
        ) from exc
    return xcals.get_calendar("XNYS", start=start.isoformat(), end=end.isoformat())


def _date(value: object, name: str) -> date:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"{name} must be a valid date")
    return pd.Timestamp(parsed).date()


def _to_datetime(value: object) -> datetime:
    parsed = pd.Timestamp(value)
    if parsed.tzinfo is None:
        parsed = parsed.tz_localize("UTC")
    return parsed.tz_convert("UTC").to_pydatetime()


__all__ = [
    "MarketCalendarDependencyError",
    "xnys_session_calendar",
    "xnys_trading_session",
]
