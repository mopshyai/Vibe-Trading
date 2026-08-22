# Continuous U.S. Options Analysis

This layer turns the point-in-time U.S. options research stack into a repeatable service. The starting universe is the full current U.S. symbol directory, not a hand-maintained watchlist. Expensive work is intentionally narrowed only after the cheap whole-market stage.

## What "continuous" means

Continuous analysis does **not** mean re-downloading every option contract for every U.S. security every second. That would be expensive, rate-limit prone, and statistically unnecessary for a 7-60 DTE strategy.

The service uses a staged cadence:

| Market phase | Full-universe chart scan | Focus-list options/catalyst refresh |
| --- | ---: | ---: |
| Closed | every 6h / on demand | off unless forced |
| Premarket | every 60m | every 15m |
| First 30m | every 15m | every 2m |
| Regular session | every 60m | every 5m |
| Power hour | every 30m | every 2m |
| Postmarket | every 60m | every 15m + outcome checkpoint |

An explicit `force_full=True` or CLI `--force-full` runs the same whole-market pipeline immediately. This is the path to use when a user asks to "analyze now".

## Pipeline

1. Refresh the current U.S. listed universe from Nasdaq Trader, or consume an operator-provided symbol file.
2. Read point-in-time daily OHLCV from `OptionsResearchStore` using `available_at <= as_of`.
3. Run the cross-sectional chart/liquidity screen across every symbol that has sufficient stored history.
4. Persist the strongest ~200 focus names.
5. Send only the strongest ~50 to the current option/catalyst providers.
6. Re-run the existing 4x feasibility ranking.
7. Persist the top research shortlist or `NO_TRADE`.
8. Keep broker mutation completely outside this service. The separate Alpaca paper executor still requires its EV, walk-forward, risk, market-open, and paper-profile gates.

The 200/50/10 defaults are configuration values, not fixed strategy assumptions.

## Point-in-time invariant

The whole-market stage reads only data that the store says was available by the requested analysis timestamp. A later vendor correction cannot silently rewrite an earlier research decision.

The state checkpoint contains only scheduler/research outputs. It does not contain broker credentials and does not authorize an order.

## Market calendar

Production must supply an authoritative exchange calendar/session schedule so holidays and early closes are correct. `weekday_regular_session()` exists only as a development fallback and labels itself `authoritative=False`.

The service fails closed for dates absent from an explicitly supplied authoritative calendar file.

Example calendar fragment:

```json
{
  "2026-08-24": {
    "open": "2026-08-24T09:30:00-04:00",
    "close": "2026-08-24T16:00:00-04:00",
    "trading_day": true,
    "source": "exchange-calendar"
  }
}
```

## Running one analysis now

```bash
python scripts/run_continuous_options_analysis.py \
  --store data/options.duckdb \
  --options-json data/current_options.json \
  --catalysts-json data/current_catalysts.json \
  --session-calendar-json data/us_market_sessions.json \
  --require-authoritative-calendar \
  --force-full \
  --output data/latest-options-analysis.json
```

When `--symbols-file` is omitted, the script refreshes the current U.S. listed universe from Nasdaq Trader once per U.S. session date and caches it in memory for that process. A failed refresh retains the last successfully fetched universe rather than replacing it with an empty set.

## Running continuously

Add `--loop`. The service hot-reloads the option and catalyst JSON provider outputs on each focus refresh, so independent data workers can update those files without restarting the research process.

```bash
python scripts/run_continuous_options_analysis.py \
  --store data/options.duckdb \
  --options-json data/current_options.json \
  --catalysts-json data/current_catalysts.json \
  --session-calendar-json data/us_market_sessions.json \
  --require-authoritative-calendar \
  --loop \
  --output data/latest-options-analysis.json
```

## Production wiring still required

The orchestration layer is provider-independent on purpose. A production deployment still needs workers that continuously populate:

- point-in-time U.S. equity bars in DuckDB,
- current option candidate data for the focus list,
- point-in-time catalyst/news scores,
- authoritative exchange sessions,
- historical outcome labels used by the EV/walk-forward layer.

Databento is the current historical research choice. The current repo also has an Alpaca connector for market/account reads and paper execution. Provider outputs should be normalized before they enter the strategy layer; the strategy should not depend directly on a vendor SDK.

## Safety boundary

This service never submits or cancels an order. Continuous research and continuous trading are intentionally different systems. A high score is not permission to trade, and a +300% target remains a payoff scenario rather than a promised or predicted return.
