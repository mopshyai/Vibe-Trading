"""Safeguards for the $1,000 weekly Alpaca paper strategy."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


SCRIPT = Path(__file__).parents[2] / "scripts" / "paper_weekly_trade_1000.py"
SPEC = importlib.util.spec_from_file_location("weekly_paper_automation", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
automation = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = automation
SPEC.loader.exec_module(automation)


def test_weekly_order_respects_notional_and_risk_caps() -> None:
    terms = automation.order_terms(100.0, 2.0)

    assert terms is not None
    assert terms["notional"] <= automation.NOTIONAL_CAP
    assert terms["planned_risk"] <= automation.RISK_CAP


def test_weekly_order_rejects_risk_larger_than_budget() -> None:
    assert automation.order_terms(500.0, 20.0) is None


def test_weekly_strategy_is_paper_sized() -> None:
    assert automation.NOTIONAL_CAP == 1_000.0
    assert automation.RISK_CAP == 20.0
    assert automation.MOMENTUM_DAYS == 5
    assert automation.TREND_DAYS == 50
    assert automation.ATR_MULTIPLE == 2.0
