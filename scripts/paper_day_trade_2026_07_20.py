#!/usr/bin/env python3
"""One-day, paper-only opening-range trader for 2026-07-20.

The script refuses to run unless the saved Alpaca profile is exactly ``paper``.
It watches ten liquid, catalyst-driven symbols, opens at most two positions after
the first five minutes, uses bracket exits, and closes its symbols before the
market close. It never targets Alpaca's live endpoint.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from statistics import median
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, time as clock_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, OrderStatus, OrderType, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrderByIdRequest,
    GetOrdersRequest,
    MarketOrderRequest,
    ReplaceOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

ET = ZoneInfo("America/New_York")
TARGET_DATE = date(2026, 7, 20)
CONFIG_PATH = Path.home() / ".vibe-trading" / "alpaca.json"
STATE_DIR = Path.home() / ".vibe-trading" / "automation" / TARGET_DATE.isoformat()
STATE_PATH = STATE_DIR / "state.json"
LOG_PATH = STATE_DIR / "paper-day-trader.log"

WATCHLIST = {
    "USO": "dynamic",
    "XLE": "dynamic",
    "XOM": "dynamic",
    "DPZ": "dynamic",
    "NFLX": "dynamic",
    "QQQ": "dynamic",
    "NVDA": "dynamic",
    "TSM": "dynamic",
    "AMD": "dynamic",
    "INTC": "dynamic",
}

MAX_POSITIONS = 2
RISK_FRACTION = 0.0025
MAX_DAILY_LOSS_FRACTION = 0.005
MAX_NOTIONAL_FRACTION = 0.10
MIN_STOP_FRACTION = 0.003
MIN_OPENING_RANGE_FRACTION = 0.002
MAX_OPENING_RANGE_FRACTION = 0.025
MIN_RELATIVE_OPENING_VOLUME = 2.0
CONFIRMATION_BARS = 2
ENTRY_END = clock_time(10, 30)
FLATTEN_TIME = clock_time(15, 50)
POLL_SECONDS = 20


@dataclass(frozen=True)
class Signal:
    symbol: str
    side: str
    price: float
    opening_high: float
    opening_low: float
    vwap: float
    score: float


@dataclass(frozen=True)
class OrderPlan:
    side: OrderSide
    quantity: int
    stop_price: float
    target_price: float


def breakeven_stop_price(side: str, filled_price: float) -> float:
    """Return a cent-rounded stop that does not turn breakeven into a loss."""
    cents = filled_price * 100
    return (math.floor(cents) if side == "long" else math.ceil(cents)) / 100


def setup_logging() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stdout)],
    )


def load_clients() -> tuple[TradingClient, StockHistoricalDataClient]:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if cfg.get("profile") != "paper":
        raise RuntimeError("refusing to run: Alpaca profile is not exactly 'paper'")
    api_key = str(cfg.get("api_key") or "").strip()
    secret_key = str(cfg.get("secret_key") or "").strip()
    if not api_key or not secret_key:
        raise RuntimeError("Alpaca paper credentials are missing")
    return (
        TradingClient(api_key, secret_key, paper=True),
        StockHistoricalDataClient(api_key, secret_key),
    )


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"symbols": [], "orders": [], "starting_equity": None}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temp = STATE_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temp.replace(STATE_PATH)


def market_window(now: datetime) -> tuple[datetime, datetime]:
    open_at = datetime.combine(TARGET_DATE, clock_time(9, 30), ET)
    return open_at, now


def opening_volume_baselines(data: StockHistoricalDataClient, now: datetime) -> dict[str, float]:
    response = data.get_stock_bars(
        StockBarsRequest(
            symbol_or_symbols=list(WATCHLIST),
            timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=now - timedelta(days=40),
            end=datetime.combine(now.date(), clock_time(9, 30), ET),
            feed=DataFeed.IEX,
        )
    )
    baselines: dict[str, float] = {}
    for symbol in WATCHLIST:
        opening_volumes = [
            float(bar.volume)
            for bar in response.data.get(symbol, [])
            if bar.timestamp.astimezone(ET).time() == clock_time(9, 30)
        ]
        baselines[symbol] = median(opening_volumes[-20:]) if opening_volumes else math.inf
    return baselines


def fetch_signals(
    data: StockHistoricalDataClient,
    now: datetime,
    already: set[str],
    volume_baselines: dict[str, float],
) -> list[Signal]:
    open_at, _ = market_window(now)
    bars = data.get_stock_bars(
        StockBarsRequest(
            symbol_or_symbols=list(WATCHLIST),
            timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=open_at,
            end=now,
            feed=DataFeed.IEX,
        )
    )
    quotes = data.get_stock_latest_quote(
        StockLatestQuoteRequest(symbol_or_symbols=list(WATCHLIST), feed=DataFeed.IEX)
    )
    signals: list[Signal] = []
    completed_cutoff = now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0)
    for symbol, bias in WATCHLIST.items():
        if symbol in already:
            continue
        symbol_bars = [
            bar for bar in bars.data.get(symbol, [])
            if bar.timestamp.astimezone(ET) < completed_cutoff
        ]
        if len(symbol_bars) < 1 + CONFIRMATION_BARS:
            continue
        opening = symbol_bars[0]
        opening_high = float(opening.high)
        opening_low = float(opening.low)
        midpoint = (opening_high + opening_low) / 2
        opening_range_fraction = (opening_high - opening_low) / max(midpoint, 0.01)
        relative_opening_volume = float(opening.volume) / max(volume_baselines.get(symbol, math.inf), 1.0)
        if not MIN_OPENING_RANGE_FRACTION <= opening_range_fraction <= MAX_OPENING_RANGE_FRACTION:
            continue
        if relative_opening_volume < MIN_RELATIVE_OPENING_VOLUME:
            continue
        total_volume = sum(float(b.volume) for b in symbol_bars)
        if total_volume <= 0:
            continue
        vwap = sum(float(b.close) * float(b.volume) for b in symbol_bars) / total_volume
        quote = quotes.get(symbol)
        if quote is None:
            continue
        bid, ask = float(quote.bid_price or 0), float(quote.ask_price or 0)
        price = (bid + ask) / 2 if bid > 0 and ask > 0 else float(symbol_bars[-1].close)
        width_fraction = max((opening_high - opening_low) / max(price, 0.01), 0.0001)

        confirmation = symbol_bars[-CONFIRMATION_BARS:]
        long_break = all(float(bar.close) > opening_high and float(bar.close) > vwap for bar in confirmation)
        short_break = all(float(bar.close) < opening_low and float(bar.close) < vwap for bar in confirmation)
        side = ""
        if bias == "long" and long_break:
            side = "long"
        elif bias == "short" and short_break:
            side = "short"
        elif bias == "dynamic":
            side = "long" if long_break else "short" if short_break else ""
        if side:
            distance = (price - opening_high) / price if side == "long" else (opening_low - price) / price
            score = relative_opening_volume * (1 + distance / width_fraction)
            signals.append(Signal(symbol, side, price, opening_high, opening_low, vwap, score))
    return sorted(signals, key=lambda item: item.score, reverse=True)


def build_order_plan(signal: Signal, equity: float) -> OrderPlan | None:
    if signal.side == "long":
        stop_price = round(signal.opening_low - 0.01, 2)
        risk_per_share = signal.price - stop_price
    else:
        stop_price = round(signal.opening_high + 0.01, 2)
        risk_per_share = stop_price - signal.price
    risk_per_share = max(risk_per_share, signal.price * MIN_STOP_FRACTION)
    risk_budget = equity * RISK_FRACTION
    qty_by_risk = math.floor(risk_budget / risk_per_share)
    qty_by_notional = math.floor((equity * MAX_NOTIONAL_FRACTION) / signal.price)
    qty = min(qty_by_risk, qty_by_notional)
    if qty < 1:
        return None

    if signal.side == "long":
        side = OrderSide.BUY
        target_price = round(signal.price + 2 * risk_per_share, 2)
    else:
        side = OrderSide.SELL
        target_price = round(signal.price - 2 * risk_per_share, 2)
    if stop_price <= 0 or target_price <= 0:
        return None
    return OrderPlan(side, qty, stop_price, target_price)


def submit_signal(trading: TradingClient, signal: Signal, equity: float) -> dict | None:
    plan = build_order_plan(signal, equity)
    if plan is None:
        logging.warning("skip %s: invalid order plan or calculated quantity is zero", signal.symbol)
        return None

    client_order_id = f"vt-paper-{TARGET_DATE:%Y%m%d}-{signal.symbol.lower()}-{int(time.time())}"
    order = trading.submit_order(
        MarketOrderRequest(
            symbol=signal.symbol,
            qty=plan.quantity,
            side=plan.side,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=plan.target_price),
            stop_loss=StopLossRequest(stop_price=plan.stop_price),
            client_order_id=client_order_id,
        )
    )
    logging.info(
        "submitted PAPER bracket symbol=%s side=%s qty=%s reference=%.2f stop=%.2f target=%.2f id=%s",
        signal.symbol,
        signal.side,
        plan.quantity,
        signal.price,
        plan.stop_price,
        plan.target_price,
        order.id,
    )
    return {
        "parent_order_id": str(order.id),
        "symbol": signal.symbol,
        "side": signal.side,
        "initial_stop": plan.stop_price,
    }


def manage_breakeven_stops(
    trading: TradingClient,
    data: StockHistoricalDataClient,
    state: dict,
) -> None:
    """Move upgraded orders to breakeven after +1R; legacy orders are ignored."""
    managed = state.get("managed_orders", [])
    moved = set(state.get("breakeven_moved", []))
    pending = [item for item in managed if item.get("parent_order_id") not in moved]
    if not pending:
        return
    quotes = data.get_stock_latest_quote(
        StockLatestQuoteRequest(
            symbol_or_symbols=sorted({item["symbol"] for item in pending}),
            feed=DataFeed.IEX,
        )
    )
    for item in pending:
        parent_id = item["parent_order_id"]
        parent = trading.get_order_by_id(parent_id)
        if parent.status != OrderStatus.FILLED or parent.filled_avg_price is None:
            continue
        entry = float(parent.filled_avg_price)
        initial_stop = float(item["initial_stop"])
        risk = entry - initial_stop if item["side"] == "long" else initial_stop - entry
        quote = quotes.get(item["symbol"])
        if risk <= 0 or quote is None:
            continue
        current = float(quote.bid_price or 0) if item["side"] == "long" else float(quote.ask_price or 0)
        reached_one_r = current >= entry + risk if item["side"] == "long" else 0 < current <= entry - risk
        if not reached_one_r:
            continue
        nested = trading.get_order_by_id(parent_id, GetOrderByIdRequest(nested=True))
        stop_leg = next(
            (
                leg
                for leg in (nested.legs or [])
                if leg.type == OrderType.STOP and leg.status in {OrderStatus.NEW, OrderStatus.HELD}
            ),
            None,
        )
        if stop_leg is None:
            continue
        new_stop = breakeven_stop_price(item["side"], entry)
        trading.replace_order_by_id(stop_leg.id, ReplaceOrderRequest(stop_price=new_stop))
        moved.add(parent_id)
        state["breakeven_moved"] = sorted(moved)
        save_state(state)
        logging.info(
            "moved PAPER stop to breakeven symbol=%s entry=%.2f stop=%.2f parent=%s",
            item["symbol"],
            entry,
            new_stop,
            parent_id,
        )


def flatten_automation_positions(trading: TradingClient, state: dict) -> None:
    symbols = set(state.get("symbols", []))
    if not symbols:
        logging.info("no automation positions to flatten")
        return
    try:
        open_orders = trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True))
        for order in open_orders:
            if str(order.symbol) in symbols:
                try:
                    trading.cancel_order_by_id(order.id)
                except Exception as exc:  # noqa: BLE001
                    logging.warning("could not cancel %s order %s: %s", order.symbol, order.id, exc)
        for symbol in symbols:
            try:
                trading.close_position(symbol)
                logging.info("flattened PAPER position %s", symbol)
            except Exception as exc:  # noqa: BLE001
                logging.info("no open %s position to flatten: %s", symbol, exc)
    finally:
        state["flattened_at"] = datetime.now(ET).isoformat()
        save_state(state)


def self_test() -> int:
    trading, data = load_clients()
    account = trading.get_account()
    if str(account.status).lower().split(".")[-1] != "active":
        raise RuntimeError(f"paper account is not active: {account.status}")
    data.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=["SPY"], feed=DataFeed.IEX))
    print("paper_mode=verified")
    print(f"account_status={account.status}")
    print(f"equity={account.equity}")
    print("watchlist=" + ",".join(WATCHLIST))
    print(f"max_positions={MAX_POSITIONS}")
    return 0


def run() -> int:
    now = datetime.now(ET)
    if now.date() != TARGET_DATE:
        logging.info("no-op: target date is %s, current date is %s", TARGET_DATE, now.date())
        return 0
    trading, data = load_clients()
    account = trading.get_account()
    state = load_state()
    if state.get("starting_equity") is None:
        state["starting_equity"] = float(account.equity)
        save_state(state)
    start_equity = float(state["starting_equity"])
    volume_baselines = opening_volume_baselines(data, now)
    open_at = datetime.combine(TARGET_DATE, clock_time(9, 35), ET)
    if now < open_at:
        time.sleep((open_at - now).total_seconds())

    while True:
        now = datetime.now(ET)
        if now.time() >= FLATTEN_TIME:
            flatten_automation_positions(trading, state)
            return 0
        account = trading.get_account()
        equity = float(account.equity)
        if equity <= start_equity * (1 - MAX_DAILY_LOSS_FRACTION):
            logging.warning("daily paper loss guard triggered at equity %.2f", equity)
            flatten_automation_positions(trading, state)
            return 0
        try:
            manage_breakeven_stops(trading, data, state)
        except Exception as exc:  # noqa: BLE001
            logging.exception("paper breakeven management failed; original stop remains active: %s", exc)
        if now.time() <= ENTRY_END and len(state.get("symbols", [])) < MAX_POSITIONS:
            try:
                signals = fetch_signals(data, now, set(state.get("symbols", [])), volume_baselines)
            except Exception as exc:  # noqa: BLE001
                logging.exception("paper market-data scan failed; retrying: %s", exc)
                time.sleep(POLL_SECONDS)
                continue
            for signal in signals:
                if len(state.get("symbols", [])) >= MAX_POSITIONS:
                    break
                try:
                    managed_order = submit_signal(trading, signal, equity)
                except Exception as exc:  # noqa: BLE001
                    logging.exception("paper order submission failed for %s; continuing: %s", signal.symbol, exc)
                    continue
                if managed_order:
                    state.setdefault("symbols", []).append(signal.symbol)
                    state.setdefault("orders", []).append(managed_order["parent_order_id"])
                    state.setdefault("managed_orders", []).append(managed_order)
                    save_state(state)
        time.sleep(POLL_SECONDS)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("run", "flatten", "self-test"), nargs="?", default="run")
    args = parser.parse_args()
    setup_logging()
    if args.mode == "self-test":
        return self_test()
    trading, _ = load_clients()
    if args.mode == "flatten":
        flatten_automation_positions(trading, load_state())
        return 0
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
