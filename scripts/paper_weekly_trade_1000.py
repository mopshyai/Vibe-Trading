#!/usr/bin/env python3
"""One-position-per-week momentum strategy for Alpaca paper trading."""

from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    StopLossRequest,
)

ET = ZoneInfo("America/New_York")
CONFIG_PATH = Path.home() / ".vibe-trading" / "alpaca.json"
SYMBOLS = ["USO", "XLE", "XOM", "DPZ", "NFLX", "QQQ", "NVDA", "TSM", "AMD", "INTC"]
NOTIONAL_CAP = 1_000.0
RISK_CAP = 20.0
MOMENTUM_DAYS = 5
TREND_DAYS = 50
ATR_DAYS = 14
ATR_MULTIPLE = 2.0


def clients() -> tuple[TradingClient, StockHistoricalDataClient]:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if cfg.get("profile") != "paper":
        raise RuntimeError("refusing to run: Alpaca profile is not paper")
    key = str(cfg.get("api_key") or "").strip()
    secret = str(cfg.get("secret_key") or "").strip()
    if not key or not secret:
        raise RuntimeError("Alpaca paper credentials are missing")
    return TradingClient(key, secret, paper=True), StockHistoricalDataClient(key, secret)


def week_key(now: datetime | None = None) -> str:
    current = (now or datetime.now(ET)).date()
    year, week, _ = current.isocalendar()
    return f"{year}-W{week:02d}"


def state_path(now: datetime | None = None) -> Path:
    return Path.home() / ".vibe-trading" / "automation" / "weekly" / week_key(now) / "weekly-1000-state.json"


def write_state(payload: dict) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def average_true_range(bars: list, length: int = ATR_DAYS) -> float:
    values = []
    for index in range(max(1, len(bars) - length), len(bars)):
        current, previous = bars[index], bars[index - 1]
        values.append(
            max(
                float(current.high) - float(current.low),
                abs(float(current.high) - float(previous.close)),
                abs(float(current.low) - float(previous.close)),
            )
        )
    return mean(values) if values else 0.0


def order_terms(entry: float, atr_value: float) -> dict | None:
    per_share_risk = atr_value * ATR_MULTIPLE
    stop = math.floor((entry - per_share_risk) * 100) / 100
    if entry <= 0 or stop <= 0 or per_share_risk <= 0:
        return None
    qty = min(math.floor(NOTIONAL_CAP / entry), math.floor(RISK_CAP / (entry - stop)))
    if qty < 1:
        return None
    return {
        "entry": entry,
        "stop": stop,
        "quantity": qty,
        "notional": round(qty * entry, 2),
        "planned_risk": round(qty * (entry - stop), 2),
    }


def candidates(data: StockHistoricalDataClient, now: datetime, excluded: set[str]) -> list[dict]:
    response = data.get_stock_bars(
        StockBarsRequest(
            symbol_or_symbols=SYMBOLS,
            timeframe=TimeFrame.Day,
            start=now - timedelta(days=120),
            end=now,
            feed=DataFeed.IEX,
        )
    )
    quotes = data.get_stock_latest_quote(
        StockLatestQuoteRequest(symbol_or_symbols=SYMBOLS, feed=DataFeed.IEX)
    )
    output = []
    for symbol in SYMBOLS:
        if symbol in excluded:
            continue
        bars = [
            bar
            for bar in response.data.get(symbol, [])
            if bar.timestamp.astimezone(ET).date() < now.date()
        ]
        if len(bars) < TREND_DAYS + 1:
            continue
        closes = [float(bar.close) for bar in bars]
        prior_close = closes[-1]
        trend = mean(closes[-TREND_DAYS:])
        momentum = prior_close / closes[-1 - MOMENTUM_DAYS] - 1
        if prior_close <= trend or momentum <= 0:
            continue
        quote = quotes.get(symbol)
        if quote is None:
            continue
        ask = float(quote.ask_price or 0)
        if ask <= 0:
            continue
        entry = math.ceil(ask * 100) / 100
        terms = order_terms(entry, average_true_range(bars))
        if terms is None:
            continue
        output.append(
            {
                "symbol": symbol,
                "momentum": momentum,
                **terms,
            }
        )
    return sorted(output, key=lambda item: item["momentum"], reverse=True)


def enter() -> int:
    now = datetime.now(ET)
    if state_path(now).exists():
        print("NO_TRADE weekly_trade_already_recorded", flush=True)
        return 0
    trading, data = clients()
    if not trading.get_clock().is_open:
        print("NO_TRADE market_closed", flush=True)
        return 0
    occupied = {str(position.symbol) for position in trading.get_all_positions()}
    occupied.update(str(order.symbol) for order in trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN)))
    ranked = candidates(data, now, occupied)
    print(f"WEEKLY_SCAN {now.isoformat()} candidates={len(ranked)}", flush=True)
    if not ranked:
        print("NO_TRADE no_weekly_signal", flush=True)
        return 0
    selected = ranked[0]
    order = trading.submit_order(
        LimitOrderRequest(
            symbol=selected["symbol"],
            qty=selected["quantity"],
            side=OrderSide.BUY,
            limit_price=selected["entry"],
            # Weekly protection must survive the entry session until Friday.
            time_in_force=TimeInForce.GTC,
            order_class=OrderClass.OTO,
            stop_loss=StopLossRequest(stop_price=selected["stop"]),
            client_order_id=(
                f"vt-paper-weekly-1000-{now:%Y%m%d}-{selected['symbol'].lower()}-{int(time.time())}"
            ),
        )
    )
    payload = {
        "week": week_key(now),
        "profile": "paper",
        "symbol": selected["symbol"],
        "side": "long",
        "quantity": selected["quantity"],
        "entry_limit": selected["entry"],
        "stop": selected["stop"],
        "maximum_notional": selected["notional"],
        "planned_risk": selected["planned_risk"],
        "five_day_momentum": selected["momentum"],
        "parent_order_id": str(order.id),
        "submitted_at": now.isoformat(),
    }
    write_state(payload)
    print(
        f"SUBMITTED WEEKLY PAPER symbol={selected['symbol']} qty={selected['quantity']} "
        f"limit={selected['entry']:.2f} stop={selected['stop']:.2f} "
        f"notional={selected['notional']:.2f} risk={selected['planned_risk']:.2f}",
        flush=True,
    )
    return 0


def flatten() -> int:
    trading, _ = clients()
    path = state_path()
    if not path.exists():
        print("WEEKLY_FLATTEN no_weekly_trade_state", flush=True)
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
        print(f"WEEKLY_FLATTENED PAPER {symbol}", flush=True)
    except Exception:
        print(f"WEEKLY_FLATTEN no_open_position {symbol}", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("enter", "flatten"), nargs="?", default="enter")
    args = parser.parse_args()
    return enter() if args.mode == "enter" else flatten()


if __name__ == "__main__":
    raise SystemExit(main())
