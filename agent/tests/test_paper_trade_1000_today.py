"""Regression tests for the $1,000 intraday Alpaca paper monitor."""

from __future__ import annotations

from datetime import datetime
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace


SCRIPT = Path(__file__).parents[2] / "scripts" / "paper_trade_1000_today.py"
SPEC = importlib.util.spec_from_file_location("paper_trade_1000_today", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
automation = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = automation
SPEC.loader.exec_module(automation)


def test_daily_order_terms_respect_notional_and_risk_caps() -> None:
    signal = SimpleNamespace(
        price=100.0,
        opening_low=99.49,
        opening_high=100.51,
        side="long",
    )

    terms = automation.order_terms(signal)

    assert terms is not None
    qty, entry, stop, _ = terms
    assert qty * entry <= automation.NOTIONAL_CAP
    assert qty * abs(entry - stop) <= automation.RISK_CAP


def test_seconds_until_flatten_uses_hard_1550_et_deadline() -> None:
    before = datetime(2026, 8, 22, 15, 49, 30, tzinfo=automation.ET)
    at_deadline = datetime(2026, 8, 22, 15, 50, 0, tzinfo=automation.ET)
    after = datetime(2026, 8, 22, 16, 0, 0, tzinfo=automation.ET)

    assert automation.seconds_until_flatten(before) == 30
    assert automation.seconds_until_flatten(at_deadline) == 0
    assert automation.seconds_until_flatten(after) == 0


def test_monitor_lifecycle_polls_no_later_than_flatten_deadline(monkeypatch) -> None:
    remaining = iter([5.0, 0.0])
    sleeps: list[float] = []
    flattened: list[object] = []
    trading = object()

    monkeypatch.setattr(automation, "seconds_until_flatten", lambda: next(remaining))
    monkeypatch.setattr(automation.time, "sleep", sleeps.append)
    monkeypatch.setattr(
        automation,
        "flatten_trade",
        lambda client: flattened.append(client) or 0,
    )

    assert automation.monitor_until_flatten(trading) == 0
    assert sleeps == [5.0]
    assert flattened == [trading]
