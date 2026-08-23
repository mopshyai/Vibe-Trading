# Historical Option Execution Model

Historical strategy selection and historical execution are separate questions.

A contract can be an excellent research candidate and still fail to fill at a realistic order price. Vibe-Trading therefore does not assume that every displayed ask became an executable trade.

## Entry model

`simulate_long_option_entry(...)` treats a historical long-option candidate as a buy-limit order submitted at the recorded decision time.

The default policy is deliberately conservative:

- 1 second simulated latency;
- 120 second maximum wait;
- no automatic chase above the decision ask;
- 20% maximum eligible bid/ask spread;
- a fill is triggered only when a post-latency displayed ask is at or below the submitted buy limit;
- a triggered order fills at the submitted limit by default, not at the midpoint and not at a better displayed ask.

A less-conservative trigger-ask fill model exists only as an explicit evaluation option. It is not the default.

## Explicit versus generated limits

If the historical candidate contains an explicit `execution_limit_price` or `limit_price`, that value is treated as the actual recorded/supplied limit for retrospective evaluation.

If no explicit limit exists, the model generates:

```text
limit = decision ask × (1 + max_chase_pct)
```

An explicit historical limit that exceeds the configured automatic-chase policy is preserved but flagged with `explicit_limit_exceeds_auto_chase_policy`. This keeps actual historical/manual order intent auditable rather than silently rewriting it.

## Latency and timeout anti-lookahead

For experiment-level execution adjustment, entry-path quotes are queried with:

```text
start = decision_time
end = decision_time + max_wait_seconds
as_of = decision_time + max_wait_seconds
```

The `as_of` timestamp is critical. A provider correction that arrives later in the day or days later cannot retroactively create a historical fill.

Outcome labeling is different: it is evaluation-only and may use the experiment's later evaluation timestamp after the entry decision has already been frozen.

## Fill states

Each candidate receives one of three execution states:

- `FILLED` — an eligible ask reached the limit after latency and before timeout;
- `UNFILLED` — observable quotes existed but the limit never became marketable, or eligible quotes failed the spread gate;
- `DATA_UNAVAILABLE` — the historical quote path was not sufficiently observable to decide.

An `UNFILLED` candidate receives **no strategy outcome**. It is not allowed to inherit the attractive return of the hypothetical research candidate.

## Quote resolution and queue uncertainty

This model uses quote observations only. It does not claim to reconstruct exchange queue position.

Warnings identify important limitations:

- `quote_resolution_coarse_for_wait_window`
- `historical_quote_size_unavailable_queue_fill_unknown`
- `eligible_quotes_rejected_by_spread_gate`

A quote showing `ask <= limit` is a trigger condition, not proof that a real order would have filled. Full queue reconstruction would require materially richer market-by-order/trade/size data and venue-specific execution assumptions.

## Implementation shortfall

For a simulated fill, the report includes:

- decision bid/ask;
- submitted limit and source;
- trigger bid/ask;
- simulated fill price/time;
- wait time;
- implementation shortfall versus the decision ask in percent;
- implementation shortfall in dollars per standard 100-share option contract.

The default fill-at-limit policy intentionally prevents the backtest from taking free price improvement whenever a later displayed ask happens to be below the submitted limit.

## Post-fill outcomes

`label_filled_execution_outcome(...)` re-runs the existing long-option outcome labeler using the simulated fill price as the actual entry basis and later bids as exit/mark economics.

This means 2x/3x/4x touches and end returns can differ materially from the original research-candidate outcome that assumed the decision ask as entry.

## Experiment adjustment

`apply_execution_model_to_experiment(...)` consumes a completed historical experiment and returns:

- per-candidate execution state;
- fill-adjusted outcomes for filled candidates only;
- fill rate;
- observable fill rate excluding data-unavailable cases;
- mean implementation shortfall;
- median fill wait;
- fill-adjusted 2x/4x/full-loss outcome statistics.

`scripts/apply_historical_execution_model.py` exposes the same model for local experiment JSON files.

Use `--plan-only` to inspect the execution policy without opening the research store. The command performs no provider request or broker operation.

## Safety

Historical execution modeling is retrospective research only. It:

- does not submit, cancel, replace or close an order;
- does not read broker credentials;
- does not enable live trading;
- does not automatically change strategy/risk policy;
- does not claim a quote-triggered historical fill is certain.
