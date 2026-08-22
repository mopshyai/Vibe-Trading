"""Regression tests for the one-day Alpaca paper automation."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

from alpaca.trading.enums import OrderSide


SCRIPT = Path(__file__).parents[2] / "scripts" / "paper_day_trade_2026_07_20.py"
SPEC = importlib.util.spec_from_file_location("paper_day_trade_automation", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
automation = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = automation
SPEC.loader.exec_module(automation)


def test_long_plan_anchors_stop_below_opening_range() -> None:
    signal = automation.Signal("TEST", "long", 101.0, 100.0, 99.0, 100.5, 2.0)

    plan = automation.build_order_plan(signal, 100_000)

    assert plan is not None
    assert plan.side is OrderSide.BUY
    assert plan.stop_price == 98.99
    assert plan.stop_price < signal.price
    assert plan.target_price > signal.price
    assert plan.quantity <= 99  # 10% notional cap


def test_short_plan_anchors_stop_above_opening_range() -> None:
    signal = automation.Signal("TEST", "short", 99.0, 101.0, 100.0, 99.5, 2.0)

    plan = automation.build_order_plan(signal, 100_000)

    assert plan is not None
    assert plan.side is OrderSide.SELL
    assert plan.stop_price == 101.01
    assert plan.stop_price > signal.price
    assert plan.target_price < signal.price
    assert plan.quantity <= 101  # 10% notional cap


def test_zero_equity_cannot_create_an_order() -> None:
    signal = automation.Signal("TEST", "long", 101.0, 100.0, 99.0, 100.5, 2.0)

    assert automation.build_order_plan(signal, 0) is None


def test_improved_filters_match_selected_backtest_variant() -> None:
    assert automation.CONFIRMATION_BARS == 2
    assert automation.MIN_RELATIVE_OPENING_VOLUME == 2.0
    assert automation.MIN_OPENING_RANGE_FRACTION == 0.002
    assert automation.MAX_OPENING_RANGE_FRACTION == 0.025


def test_breakeven_stop_rounds_to_filled_entry() -> None:
    assert automation.breakeven_stop_price("long", 101.239) == 101.23
    assert automation.breakeven_stop_price("short", 101.231) == 101.24
