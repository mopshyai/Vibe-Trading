# Historical Options Research Experiments

This document defines the evidence standard for multi-session historical experiments in Vibe-Trading.

The goal is reproducible research, not an attractive backtest chart. A historical run is useful only when the system can state what information was knowable at each decision timestamp, what universe existed then, what data version was used, and what later outcome window was used for evaluation.

## Anti-lookahead contract

Selection and outcome labeling are separate phases.

For each research timestamp:

1. equity bars are queried with `available_at <= research_time`;
2. option quotes used for selection are queried with `available_at <= research_time`;
3. the historical U.S. universe is resolved from the latest universe snapshot whose `available_at <= research_time`;
4. only after the configured fixed outcome horizon has matured may later option bids be used to label the decision;
5. future outcome fields are never written back into the feature row used for selection.

The experiment output carries an explicit anti-lookahead warning and lineage metadata.

## Universe / survivorship policy

Preferred mode is `point_in_time_snapshots`.

Each universe snapshot must contain:

```json
{
  "available_at": "2026-01-02T13:00:00+00:00",
  "source": "historical-symbol-master-v1",
  "symbols": ["AAPL.US", "MSFT.US"]
}
```

The experiment uses the latest snapshot that was available at the historical research timestamp.

A static symbol list is rejected by default. It can be used only with `--allow-static-universe`, and the output is explicitly labeled `static_explicitly_allowed` plus `static_universe_survivorship_bias_possible`. A static run must never be represented as survivorship-safe.

The exact ordered universe history is hashed into `universe_fingerprint`, and that fingerprint participates in the deterministic experiment ID.

## Code and data lineage

Every run records:

- strategy version;
- model version;
- risk-policy version;
- payoff-policy version;
- code commit SHA when supplied;
- `data_snapshot_id` when supplied;
- exact universe fingerprint;
- replay and experiment configuration;
- research timestamps and later evaluation timestamp.

For durable comparison across runs, assign an immutable `data_snapshot_id` to the market-data backfill or dataset used. Runs without it are allowed for exploration but are marked `data_snapshot_id_missing_reproducibility_weaker`.

## Session schedule

`scripts/run_options_historical_experiment.py` uses the authoritative XNYS calendar rather than assuming every weekday is a normal session.

Research timestamps are defined relative to the actual session close, so holidays and early closes are respected. The default is 30 minutes before close. A configuration that pushes the research timestamp before the session open is rejected.

Use `--plan-only` first to inspect the historical schedule and universe mode. Plan-only does not require the research-store file to exist and does not call a market-data provider or broker.

## Fixed-horizon outcomes

The default outcome horizon is 30 calendar days, capped by option expiration when earlier.

Long-option economics are intentionally conservative:

- entry uses the historical ask stored on the selected candidate;
- later evaluation uses historical bids;
- 2x / 3x / 4x touches, maximum favorable/adverse excursion, end return and full-loss proxy are recorded;
- a candidate remains immature until the fixed horizon has elapsed;
- insufficient future quote observations produce a skipped outcome, not an invented result.

The configured +300% profit objective means a 4x option-premium target. It is a payoff label, not a forecast or guarantee.

## Historical volatility-surface limitation

The current Databento `OPRA.PILLAR / cbbo-1m` adapter stores bid/ask history but does not currently populate historical implied volatility or Greeks. Therefore CBBO history by itself is not enough to reconstruct the full volatility surface used by current research.

The experiment runner fails closed here:

- if historical IV is unavailable, it emits `historical_implied_volatility_unavailable`;
- it does not infer a zero-rate Black-Scholes IV approximation;
- it does not fabricate historical skew, term structure or surface efficiency.

A production surface replay requires a defensible point-in-time source for IV/Greeks or a separately reviewed pricing model with the required rates, dividends, exercise-style and corporate-action treatment.

This limitation also affects historical option selection when the replay layer requires IV. A CBBO-only archive should not be described as a complete production backtest.

## Descriptive evidence, not automatic policy changes

Experiment summaries report outcomes by dimensions such as:

- direction;
- setup type;
- final rank;
- ranking-score bucket;
- required move versus one-sigma implied move;
- volatility-surface efficiency;
- required move versus surface expected move;
- term structure, skew and IV-vs-realized state when historically available.

Buckets are marked valid only after the configured minimum sample count. The default is 30, which is still a minimum data-quality gate rather than proof of predictive stability.

The experiment runner never promotes, retires or rewrites strategy rules automatically. Any policy change remains an explicit versioned decision and should be supported by later unseen / walk-forward evidence.

## Safety and cost boundary

Historical experiments:

- read the local point-in-time DuckDB store only;
- do not download Databento history;
- do not place, cancel, replace or close broker orders;
- do not read or write broker credentials;
- do not enable live trading.

Historical provider backfills remain a separate explicit action because they may incur data-provider charges.
