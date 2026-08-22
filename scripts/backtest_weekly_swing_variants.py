#!/usr/bin/env python3
"""Research-only backtest for one-position-per-week swing variants."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

ET = ZoneInfo("America/New_York")
SYMBOLS = ["USO", "XLE", "XOM", "DPZ", "NFLX", "QQQ", "NVDA", "TSM", "AMD", "INTC"]
START = datetime(2024, 1, 1, tzinfo=ET)
END = datetime(2026, 7, 27, tzinfo=ET)
ROUND_TRIP_SLIPPAGE = 0.001


@dataclass(frozen=True)
class Variant:
    name: str
    breakout_days: int
    fast_ma: int
    slow_ma: int
    min_relative_volume: float
    atr_multiple: float
    target_r: float


VARIANTS = [
    Variant("break5_atr1", 5, 10, 30, 0.0, 1.0, 2.0),
    Variant("break10_atr1", 10, 20, 50, 0.0, 1.0, 2.0),
    Variant("break10_rvol_atr1", 10, 20, 50, 1.2, 1.0, 2.0),
    Variant("break10_rvol_atr1.5", 10, 20, 50, 1.2, 1.5, 2.0),
    Variant("break20_rvol_atr1", 20, 20, 50, 1.2, 1.0, 2.0),
    Variant("break10_rvol_1.5r", 10, 20, 50, 1.2, 1.0, 1.5),
]


def fetch() -> dict[str, list]:
    cfg = json.loads((Path.home() / ".vibe-trading" / "alpaca.json").read_text(encoding="utf-8"))
    if cfg.get("profile") != "paper":
        raise RuntimeError("research must use the paper credential profile")
    client = StockHistoricalDataClient(cfg["api_key"], cfg["secret_key"])
    response = client.get_stock_bars(
        StockBarsRequest(
            symbol_or_symbols=SYMBOLS,
            timeframe=TimeFrame.Day,
            start=START,
            end=END,
            feed=DataFeed.IEX,
        )
    )
    return {symbol: sorted(response.data.get(symbol, []), key=lambda bar: bar.timestamp) for symbol in SYMBOLS}


def atr(bars: list, end: int, length: int = 14) -> float:
    values = []
    for index in range(max(1, end - length + 1), end + 1):
        current, previous = bars[index], bars[index - 1]
        values.append(
            max(
                float(current.high) - float(current.low),
                abs(float(current.high) - float(previous.close)),
                abs(float(current.low) - float(previous.close)),
            )
        )
    return mean(values) if values else 0.0


def candidate(bars: list, entry_index: int, variant: Variant) -> dict | None:
    history_end = entry_index - 1
    required = max(variant.slow_ma, variant.breakout_days, 20)
    if history_end < required:
        return None
    closes = [float(bar.close) for bar in bars]
    volumes = [float(bar.volume) for bar in bars]
    prior = closes[history_end]
    fast = mean(closes[history_end - variant.fast_ma + 1 : history_end + 1])
    slow = mean(closes[history_end - variant.slow_ma + 1 : history_end + 1])
    previous_window = closes[history_end - variant.breakout_days : history_end]
    relative_volume = volumes[history_end] / max(mean(volumes[history_end - 20 : history_end]), 1.0)
    if relative_volume < variant.min_relative_volume:
        return None
    side = ""
    if prior > fast > slow and prior > max(previous_window):
        side = "long"
    elif prior < fast < slow and prior < min(previous_window):
        side = "short"
    if not side:
        return None
    entry = float(bars[entry_index].open)
    risk = atr(bars, history_end) * variant.atr_multiple
    if entry <= 0 or risk <= 0:
        return None
    momentum = abs(prior / closes[history_end - 20] - 1)
    return {"side": side, "entry": entry, "risk": risk, "score": relative_volume * (1 + momentum)}


def outcome(signal: dict, bars: list, entry_index: int, variant: Variant) -> float:
    entry, risk, side = signal["entry"], signal["risk"], signal["side"]
    stop = entry - risk if side == "long" else entry + risk
    target = entry + variant.target_r * risk if side == "long" else entry - variant.target_r * risk
    exit_price = float(bars[entry_index].close)
    for bar in bars[entry_index:]:
        if bar.timestamp.date().isocalendar()[:2] != bars[entry_index].timestamp.date().isocalendar()[:2]:
            break
        high, low = float(bar.high), float(bar.low)
        if side == "long":
            if low <= stop:
                exit_price = stop
                break
            if high >= target:
                exit_price = target
                break
        else:
            if high >= stop:
                exit_price = stop
                break
            if low <= target:
                exit_price = target
                break
        exit_price = float(bar.close)
        if bar.timestamp.astimezone(ET).weekday() == 4:
            break
    gross_r = (exit_price - entry) / risk if side == "long" else (entry - exit_price) / risk
    return gross_r - entry * ROUND_TRIP_SLIPPAGE / risk


def results(data: dict[str, list], variant: Variant) -> list[tuple[date, float]]:
    by_day: dict[date, list[tuple[str, int, dict]]] = {}
    for symbol, bars in data.items():
        for index, bar in enumerate(bars):
            day = bar.timestamp.astimezone(ET).date()
            if day.weekday() > 4:
                continue
            signal = candidate(bars, index, variant)
            if signal:
                by_day.setdefault(day, []).append((symbol, index, signal))
    used_weeks: set[tuple[int, int]] = set()
    output = []
    for day in sorted(by_day):
        week = day.isocalendar()[:2]
        if week in used_weeks:
            continue
        ranked = sorted(by_day[day], key=lambda item: item[2]["score"], reverse=True)
        symbol, index, signal = ranked[0]
        output.append((day, outcome(signal, data[symbol], index, variant)))
        used_weeks.add(week)
    return output


def metrics(rows: list[tuple[date, float]]) -> dict:
    values = [value for _, value in rows]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value <= 0]
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "trades": len(values),
        "win_rate": len(wins) / len(values) if values else 0,
        "avg_r": mean(values) if values else 0,
        "total_r": sum(values),
        "max_dd_r": drawdown,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses and sum(losses) else 0,
    }


def selected_momentum_results(data: dict[str, list]) -> list[tuple[date, float]]:
    """First trading day: buy strongest positive 5-day momentum above MA50."""
    candidates_by_week: dict[tuple[int, int], list[tuple[float, str, int]]] = {}
    for symbol, bars in data.items():
        closes = [float(bar.close) for bar in bars]
        for index, bar in enumerate(bars):
            day = bar.timestamp.astimezone(ET).date()
            week = day.isocalendar()[:2]
            if index < 51:
                continue
            if bars[index - 1].timestamp.astimezone(ET).date().isocalendar()[:2] == week:
                continue
            prior = closes[index - 1]
            momentum = prior / closes[index - 6] - 1
            if prior > mean(closes[index - 50 : index]) and momentum > 0:
                candidates_by_week.setdefault(week, []).append((momentum, symbol, index))

    rows = []
    for week in sorted(candidates_by_week):
        _, symbol, entry_index = max(candidates_by_week[week])
        bars = data[symbol]
        entry = float(bars[entry_index].open)
        risk = atr(bars, entry_index - 1) * 2
        stop = entry - risk
        exit_price = float(bars[entry_index].close)
        for bar in bars[entry_index:]:
            if bar.timestamp.astimezone(ET).date().isocalendar()[:2] != week:
                break
            if float(bar.low) <= stop:
                exit_price = stop
                break
            exit_price = float(bar.close)
        result_r = (exit_price - entry) / risk - entry * ROUND_TRIP_SLIPPAGE / risk
        rows.append((bars[entry_index].timestamp.astimezone(ET).date(), result_r))
    return rows


def main() -> int:
    data = fetch()
    cutoff = date(2026, 1, 1)
    print("variant,trades,win_rate,avg_r,total_r,max_dd_r,profit_factor,oos_trades,oos_avg_r,oos_total_r,oos_max_dd_r")
    for variant in VARIANTS:
        rows = results(data, variant)
        all_metrics = metrics(rows)
        oos = metrics([row for row in rows if row[0] >= cutoff])
        print(
            f"{variant.name},{all_metrics['trades']},{all_metrics['win_rate']:.1%},{all_metrics['avg_r']:.3f},"
            f"{all_metrics['total_r']:.2f},{all_metrics['max_dd_r']:.2f},{all_metrics['profit_factor']:.2f},"
            f"{oos['trades']},{oos['avg_r']:.3f},{oos['total_r']:.2f},{oos['max_dd_r']:.2f}"
        )
    rows = selected_momentum_results(data)
    selected = metrics(rows)
    selected_oos = metrics([row for row in rows if row[0] >= cutoff])
    print(
        f"selected_5d_momentum_ma50,{selected['trades']},{selected['win_rate']:.1%},{selected['avg_r']:.3f},"
        f"{selected['total_r']:.2f},{selected['max_dd_r']:.2f},{selected['profit_factor']:.2f},"
        f"{selected_oos['trades']},{selected_oos['avg_r']:.3f},{selected_oos['total_r']:.2f},"
        f"{selected_oos['max_dd_r']:.2f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
