# U.S. Options Market Engine — Build Plan

## Objective

Turn Vibe-Trading from a general-purpose trading agent into a research-first engine that can scan the U.S. listed market, identify unusually asymmetric long-option setups, and rank only the contracts that have a plausible path to a configured payoff target.

The default research target is **+300% profit**, which means **4x the entry premium**. That is a payoff constraint, not a forecast or guarantee.

## Design principles

The project should borrow the *publicly observable engineering principles* of top quantitative trading firms without pretending to know proprietary strategies:

1. **Probability and expected value over narratives.** A setup score is never labeled as a probability until it is empirically calibrated against out-of-sample outcomes.
2. **Research, trading, and engineering share one model of truth.** Features used in research must be the same features available point-in-time in paper/live workflows.
3. **Cheap filters before expensive computation.** Scan the full market with vectorized price/volume features, then spend options/news/model calls only on survivors.
4. **Data quality is a feature.** Reject stale, incomplete, illiquid, or structurally suspicious data before ranking.
5. **Execution realism belongs in research.** Bid/ask spread, liquidity, slippage, fill assumptions, IV crush, and contract multiplier must be modeled before an edge is believed.
6. **No-trade is a valid result.** The engine should reject the whole market when the evidence is weak.
7. **Risk is a first-class system, not a final check.** Later phases will add portfolio concentration, correlation, event overlap, max premium-at-risk, and daily loss budgets before any broker path is considered.
8. **Observability before automation.** Every candidate should be explainable from source data, features, gates, score components, and model version.

## Target architecture

```text
U.S. symbol directory
        |
        v
Bulk point-in-time OHLCV store --------------------+
        |                                           |
        v                                           |
Cross-sectional chart engine                        |
(all NASDAQ / NYSE / NYSE American / ETFs)          |
        | top ~200                                   |
        v                                           |
Catalyst + event layer                              |
(earnings, filings, news, macro/event calendar)     |
        | top ~50                                    |
        v                                           |
Options surface engine <---- historical option data+
(IV, spread, OI, volume, DTE, Greeks, skew)
        | top ~10-25
        v
300% payoff feasibility + calibrated outcome model
        |
        v
Portfolio/risk gate
        |
        +----> NO TRADE
        |
        v
Paper-trade candidate / human review
        |
        v
Post-trade attribution + model calibration
```

## Phase 1 — Whole-market chart engine (built in this PR)

### Universe

Use Nasdaq Trader's daily symbol directory to cover Nasdaq and other U.S. venues. Keep ETFs because highly liquid index/sector ETFs often have better options markets than single names. Exclude test issues. Normalize symbols to the repo's `.US` convention.

### Data transport

Signal logic is deliberately provider-independent. The existing Yahoo loader is useful for single-name development, but its per-host throttle makes it the wrong transport for repeatedly scanning thousands of symbols intraday. Production should ingest a bulk feed into Parquet/DuckDB or another point-in-time store, then hand frames to the same scanner.

### Chart features

For each symbol:

- 1d / 5d / 20d / 60d return
- 20 / 50 / 200-day moving-average structure
- 14-day ATR as percent of price
- 20-day annualized realized volatility
- RSI(14)
- 20-day relative volume and volume z-score
- gap versus prior close
- location inside the prior 20-day range
- breakout / breakdown distance
- 20-day average dollar volume

The market is ranked **cross-sectionally**, so a candidate competes with everything else available that day rather than only satisfying fixed retail-style indicator thresholds.

### Direction

The chart stage independently scores bullish and bearish structure. The stronger side maps to the option side:

- bullish -> long call candidates
- bearish -> long put candidates

A minimum score and a minimum bullish-vs-bearish score gap prevent ambiguous charts from reaching the expensive options stage.

## Phase 2 — Options asymmetry engine (foundation already built in PR #2)

The existing options-opportunity layer supplies:

- bid/ask and spread
- open interest and volume
- IV
- DTE
- max premium loss
- target premium for the configured profit goal
- stock move required to reach that premium at expiry
- required move versus the contract's one-sigma IV-implied move

The market pipeline now combines chart direction with same-direction calls/puts and rejects contracts whose required move is too large relative to implied volatility.

## Phase 3 — Catalyst/event engine

Add point-in-time event features, not narrative-only LLM summaries:

- earnings date and expected move
- earnings revisions / guidance changes
- SEC filings and material 8-K events
- product/regulatory/legal events
- analyst estimate dispersion and revision velocity
- scheduled macro exposure
- news velocity and novelty

The LLM can summarize *after* structured event extraction. It should not invent the signal.

## Phase 4 — Historical options + calibration

This is the phase that converts a ranking heuristic into a real quantitative model.

For every historical candidate, store the feature vector and evaluate whether the option achieved the configured payoff target before expiry, plus the path taken to get there.

Calibrate:

- probability of touching 2x / 3x / 4x premium
- probability of losing 25% / 50% / 100% of premium first
- time-to-target distribution
- realized slippage versus quoted spread
- outcome by DTE, moneyness, IV percentile, event type, market regime, sector, and setup type

Use walk-forward evaluation and time-based folds. Never random-shuffle market history. Control survivorship bias and corporate actions.

## Phase 5 — Expected value and portfolio construction

A 4x target alone is insufficient. A trade can have huge upside and still have negative expected value.

The decision layer should estimate a discrete payoff distribution and compare expected value after costs. It should also cap:

- premium at risk per position
- total premium at risk
- correlated exposure by sector/index/theme
- same-event concentration
- same-expiry concentration
- daily and weekly drawdown budgets

The output remains **NO TRADE** when post-cost expected value or calibration quality is insufficient.

## Phase 6 — Paper execution and attribution

Only after calibration:

1. generate a paper candidate;
2. record the exact market snapshot and model version;
3. model limit-price entry rather than assuming ask fills;
4. record fill quality, adverse selection, and missed fills;
5. track exit rules and target path;
6. compare predicted distribution with realized outcome.

Live broker execution is deliberately out of scope until the research and paper evidence is strong enough and the existing mandate/kill-switch/order-gate safety model is integrated explicitly.

## Data and infrastructure priorities

Spend engineering effort in this order:

1. reliable point-in-time U.S. equity and options history;
2. bulk ingestion + caching + reproducible datasets;
3. feature correctness and no-lookahead tests;
4. backtest realism and calibration;
5. monitoring/data lineage;
6. only then latency/parallelization optimizations that measurements show are needed.

For a 7-60 DTE options strategy, microsecond latency is not the first bottleneck. Bad data, survivorship bias, stale quotes, spread assumptions, and IV/event modeling can destroy far more edge than a few milliseconds.

## Success metrics

Do not optimize only for raw hit rate or the number of 300% winners. Track:

- precision among top 1 / 5 / 10 ranked candidates
- calibrated target-touch probability
- expected value after spread/slippage
- median and mean realized return
- full-loss rate
- drawdown and CVaR
- turnover and capacity
- score stability across market regimes
- degradation between backtest, paper, and live-observation environments

## Current safety boundary

The new market engine is research-only. It has no broker client and no order path. The batch runner consumes local point-in-time files and optional precomputed option candidates. Network access is limited to the separate read-only symbol-directory helper when explicitly called.
