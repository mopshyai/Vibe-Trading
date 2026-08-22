from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from src.trading_platform.market_calendar import xnys_session_calendar, xnys_trading_session

ET = ZoneInfo("America/New_York")


def test_independence_day_observed_is_closed_in_2026() -> None:
    session = xnys_trading_session(datetime(2026, 7, 3, 16, 0, tzinfo=timezone.utc))
    assert session.authoritative is True
    assert session.trading_day is False


def test_day_after_thanksgiving_2026_is_early_close() -> None:
    session = xnys_trading_session(datetime(2026, 11, 27, 15, 0, tzinfo=timezone.utc))
    assert session.trading_day is True
    assert session.open_at.astimezone(ET).time() == time(9, 30)
    assert session.close_at.astimezone(ET).time() == time(13, 0)


def test_calendar_export_marks_normal_and_early_sessions() -> None:
    calendar = xnys_session_calendar("2026-11-26", "2026-11-30")
    assert calendar["2026-11-26"]["trading_day"] is False
    assert calendar["2026-11-27"]["trading_day"] is True
    assert calendar["2026-11-27"]["early_close"] is True
    assert calendar["2026-11-30"]["early_close"] is False
