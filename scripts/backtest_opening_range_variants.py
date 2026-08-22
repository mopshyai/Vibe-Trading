#!/usr/bin/env python3
"""Compare opening-range variants on recent Alpaca IEX intraday bars.

This is research-only: it reads paper credentials for market-data access and
never creates, changes, or cancels an order.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

ET = ZoneInfo("America/New_York")
SYMBOLS = ["USO", "XLE", "XOM", "DPZ", "NFLX", "QQQ", "NVDA", "TSM", "AMD", "INTC"]
START = datetime(2026, 5, 1, tzinfo=ET)
END = datetime(2026, 7, 20, 9, 30, tzinfo=ET)
ENTRY_END = time(10, 30)
FLATTEN_AT = time(15, 50)
ROUND_TRIP_SLIPPAGE = 0.0004  # 2 bps each side


@dataclass(frozen=True)
class Variant:
    name: str
    confirmations: int
    min_relative_volume: float
    min_opening_range_pct: float
    max_opening_range_pct: float
    target_r: float
    move_stop_to_break_even: bool


VARIANTS = [
    Variant("baseline_1bar", 1, 0.0, 0.0, 1.0, 2.0, False),
    Variant("confirm_2bar", 2, 0.0, 0.0, 0.03, 2.0, False),
    Variant("confirm_relvol", 2, 1.2, 0.002, 0.025, 2.0, False),
    Variant("confirm_relvol_be", 2, 1.2, 0.002, 0.025, 2.0, True),
    Variant("confirm_relvol_1.5r", 2, 1.2, 0.002, 0.025, 1.5, False),
    Variant("onebar_relvol_be", 1, 1.2, 0.002, 0.025, 2.0, True),
    Variant("relvol_1.5_be", 2, 1.5, 0.002, 0.025, 2.0, True),
    Variant("relvol_2.0_be", 2, 2.0, 0.002, 0.025, 2.0, True),
    Variant("relvol_be_tight_range", 2, 1.2, 0.002, 0.015, 2.0, True),
    Variant("relvol_be_2.5r", 2, 1.2, 0.002, 0.025, 2.5, True),
]


def client() -> StockHistoricalDataClient:
    cfg = json.loads((Path.home() / ".vibe-trading" / "alpaca.json").read_text(encoding="utf-8"))
    if cfg.get("profile") != "paper":
        raise RuntimeError("research must use the paper credential profile")
    return StockHistoricalDataClient(cfg["api_key"], cfg["secret_key"])


def fetch_sessions() -> dict[str, dict[date, list]]:
    response = client().get_stock_bars(
        StockBarsRequest(
            symbol_or_symbols=SYMBOLS,
            timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=START,
            end=END,
            feed=DataFeed.IEX,
        )
    )
    sessions: dict[str, dict[date, list]] = {symbol: {} for symbol in SYMBOLS}
    for symbol in SYMBOLS:
        for bar in response.data.get(symbol, []):
            timestamp = bar.timestamp.astimezone(ET)
            if time(9, 30) <= timestamp.time() < time(16, 0):
                sessions[symbol].setdefault(timestamp.date(), []).append(bar)
        for bars in sessions[symbol].values():
            bars.sort(key=lambda item: item.timestamp)
    return sessions


def rolling_open_volume(sessions: dict[str, dict[date, list]]) -> dict[tuple[str, date], float]:
    result: dict[tuple[str, date], float] = {}
    for symbol, by_date in sessions.items():
        history: list[float] = []
        for session_date in sorted(by_date):
            opening_volume = float(by_date[session_date][0].volume)
            result[(symbol, session_date)] = median(history[-20:]) if history else opening_volume
            history.append(opening_volume)
    return result


def signal_for_day(symbol: str, session_date: date, bars: list, variant: Variant, volume_baseline: float):
    if len(bars) < 12:
        return None
    opening = bars[0]
    opening_high, opening_low = float(opening.high), float(opening.low)
    midpoint = (opening_high + opening_low) / 2
    opening_range_pct = (opening_high - opening_low) / max(midpoint, 0.01)
    relative_volume = float(opening.volume) / max(volume_baseline, 1.0)
    if not (variant.min_opening_range_pct <= opening_range_pct <= variant.max_opening_range_pct):
        return None
    if relative_volume < variant.min_relative_volume:
        return None

    cumulative_pv = float(opening.close) * float(opening.volume)
    cumulative_volume = float(opening.volume)
    long_count = short_count = 0
    for index, bar in enumerate(bars[1:], start=1):
        ts = bar.timestamp.astimezone(ET).time()
        if ts > ENTRY_END:
            break
        cumulative_pv += float(bar.close) * float(bar.volume)
        cumulative_volume += float(bar.volume)
        vwap = cumulative_pv / max(cumulative_volume, 1.0)
        close = float(bar.close)
        long_count = long_count + 1 if close > opening_high and close > vwap else 0
        short_count = short_count + 1 if close < opening_low and close < vwap else 0
        if long_count >= variant.confirmations:
            distance = max(close - opening_high, 0.0) / max(opening_high - opening_low, 0.01)
            return {
                "date": session_date,
                "symbol": symbol,
                "side": "long",
                "entry_index": index,
                "entry_time": bar.timestamp,
                "entry": close,
                "stop": opening_low - 0.01,
                "score": relative_volume * (1 + distance),
            }
        if short_count >= variant.confirmations:
            distance = max(opening_low - close, 0.0) / max(opening_high - opening_low, 0.01)
            return {
                "date": session_date,
                "symbol": symbol,
                "side": "short",
                "entry_index": index,
                "entry_time": bar.timestamp,
                "entry": close,
                "stop": opening_high + 0.01,
                "score": relative_volume * (1 + distance),
            }
    return None


def simulate(signal: dict, bars: list, variant: Variant) -> float | None:
    entry, stop = signal["entry"], signal["stop"]
    risk = entry - stop if signal["side"] == "long" else stop - entry
    if risk <= 0:
        return None
    target = entry + variant.target_r * risk if signal["side"] == "long" else entry - variant.target_r * risk
    active_stop = stop
    break_even_armed = False
    exit_price = None

    for bar in bars[signal["entry_index"] + 1 :]:
        if bar.timestamp.astimezone(ET).time() >= FLATTEN_AT:
            exit_price = float(bar.open)
            break
        high, low = float(bar.high), float(bar.low)
        if signal["side"] == "long":
            if low <= active_stop:
                exit_price = active_stop
                break
            if high >= target:
                exit_price = target
                break
            if variant.move_stop_to_break_even and not break_even_armed and high >= entry + risk:
                active_stop = entry
                break_even_armed = True
        else:
            if high >= active_stop:
                exit_price = active_stop
                break
            if low <= target:
                exit_price = target
                break
            if variant.move_stop_to_break_even and not break_even_armed and low <= entry - risk:
                active_stop = entry
                break_even_armed = True
    if exit_price is None:
        exit_price = float(bars[-1].close)

    gross_r = (exit_price - entry) / risk if signal["side"] == "long" else (entry - exit_price) / risk
    slippage_r = (entry * ROUND_TRIP_SLIPPAGE) / risk
    return gross_r - slippage_r


def metrics(results: list[tuple[date, float]]) -> dict[str, float]:
    values = [value for _, value in results]
    if not values:
        return {"trades": 0, "win_rate": 0, "avg_r": 0, "total_r": 0, "max_dd_r": 0, "profit_factor": 0}
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value <= 0]
    daily: dict[date, float] = {}
    for session_date, value in results:
        daily[session_date] = daily.get(session_date, 0.0) + value
    equity = peak = max_drawdown = 0.0
    for session_date in sorted(daily):
        equity += daily[session_date]
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return {
        "trades": len(values),
        "win_rate": len(wins) / len(values),
        "avg_r": sum(values) / len(values),
        "total_r": sum(values),
        "max_dd_r": max_drawdown,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses and sum(losses) else math.inf,
    }


def trade_results(sessions, volume_baselines, variant: Variant):
    candidates: dict[date, list[tuple[dict, list]]] = {}
    for symbol, by_date in sessions.items():
        for session_date, bars in by_date.items():
            signal = signal_for_day(symbol, session_date, bars, variant, volume_baselines[(symbol, session_date)])
            if signal:
                candidates.setdefault(session_date, []).append((signal, bars))
    results: list[tuple[date, float]] = []
    for session_date, day_candidates in candidates.items():
        # Match the live bot: earlier completed confirmations consume the two
        # slots first; score only ranks signals that arrive at the same time.
        ordered = sorted(
            day_candidates,
            key=lambda item: (item[0]["entry_time"], -item[0]["score"]),
        )
        for signal, bars in ordered[:2]:
            outcome = simulate(signal, bars, variant)
            if outcome is not None:
                results.append((session_date, outcome))
    return results


def main() -> int:
    sessions = fetch_sessions()
    volume_baselines = rolling_open_volume(sessions)
    print("variant,trades,win_rate,avg_r,total_r,max_dd_r,profit_factor,oos_trades,oos_avg_r,oos_total_r,oos_max_dd_r")
    for variant in VARIANTS:
        results = trade_results(sessions, volume_baselines, variant)
        row = metrics(results)
        oos = metrics([(session_date, value) for session_date, value in results if session_date >= date(2026, 6, 19)])
        print(
            f"{variant.name},{int(row['trades'])},{row['win_rate']:.1%},{row['avg_r']:.3f},"
            f"{row['total_r']:.2f},{row['max_dd_r']:.2f},{row['profit_factor']:.2f},"
            f"{int(oos['trades'])},{oos['avg_r']:.3f},{oos['total_r']:.2f},{oos['max_dd_r']:.2f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
