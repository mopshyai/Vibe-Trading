#!/usr/bin/env python3
"""One-trade Alpaca paper monitor with a hard $1,000 entry-notional cap."""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
import time
from datetime import datetime, time as clock_time
from pathlib import Path
from zoneinfo import ZoneInfo

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

ET = ZoneInfo("America/New_York")
CONFIG_PATH = Path.home() / ".vibe-trading" / "alpaca.json"
NOTIONAL_CAP = 1_000.0
RISK_CAP = 10.0
ENTRY_END = clock_time(10, 30)
FLATTEN_TIME = clock_time(15, 50)
POLL_SECONDS = 20


def load_strategy():
    path = Path(__file__).with_name("paper_day_trade_2026_07_20.py")
    spec = importlib.util.spec_from_file_location("vibe_opening_range_strategy", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load opening-range strategy")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.TARGET_DATE = datetime.now(ET).date()
    return module


def clients():
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if cfg.get("profile") != "paper":
        raise RuntimeError("refusing to run: Alpaca profile is not paper")
    strategy = load_strategy()
    trading, data = strategy.load_clients()
    return strategy, trading, data


def state_path() -> Path:
    day = datetime.now(ET).date().isoformat()
    return Path.home() / ".vibe-trading" / "automation" / day / "budget-1000-state.json"


def write_state(payload: dict) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def order_terms(signal) -> tuple[int, float, float, float] | None:
    # A limit entry makes qty * entry_price a true hard ceiling.
    entry = round(signal.price, 2)
    stop = round(signal.opening_low - 0.01, 2) if signal.side == "long" else round(signal.opening_high + 0.01, 2)
    per_share_risk = entry - stop if signal.side == "long" else stop - entry
    if entry <= 0 or stop <= 0 or per_share_risk <= 0:
        return None
    qty = min(math.floor(NOTIONAL_CAP / entry), math.floor(RISK_CAP / per_share_risk))
    if qty < 1:
        return None
    target = round(entry + 2 * per_share_risk, 2) if signal.side == "long" else round(entry - 2 * per_share_risk, 2)
    if target <= 0:
        return None
    return qty, entry, stop, target


def monitor() -> int:
    strategy, trading, data = clients()
    if state_path().exists():
        print("NO_TRADE daily_trade_already_recorded", flush=True)
        return 0
    clock = trading.get_clock()
    if not clock.is_open:
        print("NO_TRADE market_closed", flush=True)
        return 0
    occupied_symbols = {str(position.symbol) for position in trading.get_all_positions()}
    occupied_symbols.update(
        str(order.symbol) for order in trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
    )

    now = datetime.now(ET)
    baselines = strategy.opening_volume_baselines(data, now)
    while datetime.now(ET).time() <= ENTRY_END:
        now = datetime.now(ET)
        signals = strategy.fetch_signals(data, now, occupied_symbols, baselines)
        print(f"SCAN {now.isoformat()} signals={len(signals)}", flush=True)
        for signal in signals:
            terms = order_terms(signal)
            if terms is None:
                continue
            qty, entry, stop, target = terms
            side = OrderSide.BUY if signal.side == "long" else OrderSide.SELL
            order = trading.submit_order(
                LimitOrderRequest(
                    symbol=signal.symbol,
                    qty=qty,
                    side=side,
                    limit_price=entry,
                    time_in_force=TimeInForce.DAY,
                    order_class=OrderClass.BRACKET,
                    take_profit=TakeProfitRequest(limit_price=target),
                    stop_loss=StopLossRequest(stop_price=stop),
                    client_order_id=f"vt-paper-1000-{now:%Y%m%d}-{signal.symbol.lower()}-{int(time.time())}",
                )
            )
            write_state(
                {
                    "date": now.date().isoformat(),
                    "profile": "paper",
                    "symbol": signal.symbol,
                    "side": signal.side,
                    "quantity": qty,
                    "entry_limit": entry,
                    "stop": stop,
                    "target": target,
                    "maximum_notional": round(qty * entry, 2),
                    "planned_risk": round(qty * abs(entry - stop), 2),
                    "parent_order_id": str(order.id),
                    "submitted_at": now.isoformat(),
                }
            )
            print(
                f"SUBMITTED PAPER symbol={signal.symbol} side={signal.side} qty={qty} "
                f"limit={entry:.2f} stop={stop:.2f} target={target:.2f} "
                f"notional={qty * entry:.2f} risk={qty * abs(entry - stop):.2f}",
                flush=True,
            )
            return 0
        time.sleep(POLL_SECONDS)
    print("NO_TRADE no_valid_signal_before_1030", flush=True)
    return 0


def flatten() -> int:
    _, trading, _ = clients()
    path = state_path()
    if not path.exists():
        print("FLATTEN no_budget_trade_state", flush=True)
        return 0
    state = json.loads(path.read_text(encoding="utf-8"))
    symbol = state["symbol"]
    for order in trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN)):
        if str(order.symbol) == symbol:
            try:
                trading.cancel_order_by_id(order.id)
            except Exception:
                pass
    try:
        trading.close_position(symbol)
        print(f"FLATTENED PAPER {symbol}", flush=True)
    except Exception:
        print(f"FLATTEN no_open_position {symbol}", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("monitor", "flatten"), default="monitor", nargs="?")
    args = parser.parse_args()
    return monitor() if args.mode == "monitor" else flatten()


if __name__ == "__main__":
    raise SystemExit(main())
