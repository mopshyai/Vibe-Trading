# Historical OPRA Option Metadata

This slice adds point-in-time option reference/statistics data to the historical research store. The purpose is to make replay more realistic without pretending that a CBBO quote contains information it did not contain.

## Store schema v2

`OptionsResearchStore` schema v2 adds two append-only tables while retaining all v1 equity-bar and option-quote rows:

- `option_definitions`
- `option_statistics`

Opening a v1 store performs an additive migration: the new tables/indexes are created and `research_store_meta.schema_version` becomes `2`. Existing bars and quotes are not rewritten.

## Option definitions

Definition observations preserve:

- OCC/OSI contract symbol
- project underlying (for example `AAPL.US`)
- strike
- call/put
- expiration
- activation time when supplied
- minimum price increment when supplied
- contract multiplier when supplied
- update action/raw symbol
- source
- `event_ts`
- `available_at`

`option_definitions_asof(as_of=...)` returns the newest definition that was actually knowable at the requested timestamp. Later corrections or contract updates do not rewrite earlier research history.

## Open interest

Databento OPRA `statistics` records with open-interest stat type are normalized into:

```text
metric = open_interest
value = published quantity
available_at = receive/event timestamp
reference_ts = provider reference timestamp when supplied
```

Open interest is therefore usable only after the provider published it. A value published before the regular session can enrich an intraday historical quote; it is never copied backward to a prior day.

## Daily option volume

Completed-session option volume is normalized from OPRA `ohlcv-1d` into:

```text
metric = daily_volume
value = completed daily volume
available_at = actual XNYS session close
```

The availability timestamp uses the authoritative XNYS calendar, including early closes. For example, Friday November 27, 2026 closes at 13:00 ET / 18:00 UTC, so that day's completed volume becomes available at 18:00 UTC rather than a hard-coded 21:00 UTC.

This prevents a 15:30 ET research timestamp from seeing the final volume of a session that has not yet ended.

## Quote enrichment

`option_quotes_with_metadata_asof(...)` starts with the normal point-in-time quote query and then joins only metadata whose `available_at <= as_of`.

Precedence is conservative:

1. quote-native open interest or volume wins when present;
2. otherwise the latest published point-in-time statistic may fill the missing field;
3. definitions are attached only when available by the same as-of timestamp.

This is an enrichment view. Raw historical quote rows are not rewritten.

## Databento metadata adapter

`DatabentoOptionMetadataAdapter` uses explicit historical schemas:

- `definition`
- `statistics` for open interest
- `ohlcv-1d` for completed daily volume

Requests use OPRA parent symbology such as `AAPL.OPT`. Project storage retains the repository convention such as `AAPL.US`.

The adapter is market-data only. It has no broker dependency.

## Cost boundary

`scripts/backfill_option_metadata.py` is intentionally separate from the hosted trading worker. Historical provider requests may incur charges.

Use `--plan-only` first. Plan-only:

- requires no `DATABENTO_API_KEY`;
- does not create/open the research DuckDB;
- does not make a provider request;
- does not call a broker;
- shows underlying count, date window and requested schemas.

A real backfill requires `DATABENTO_API_KEY` from the runtime environment. The key is never written to the repository or store.

## Historical IV / Greeks remain unresolved

This metadata upgrade does **not** derive or fabricate implied volatility, delta, gamma, theta or vega.

The current OPRA CBBO adapter still stores historical `implied_volatility = null`. OPRA open-interest and daily-volume metadata improve liquidity realism but do not make a CBBO-only archive a complete production options replay dataset.

Until a defensible point-in-time IV/Greeks source or separately reviewed pricing model is added:

- historical replay continues to emit `historical_implied_volatility_missing` when contract scoring needs IV;
- historical surface analysis remains unavailable when IV is absent;
- a data-limited session must not be represented as a normal strategy `NO_TRADE` result.

## Safety

This slice adds storage and read-only historical market-data ingestion only. It does not:

- submit/cancel/replace broker orders;
- enable live trading;
- write credentials;
- automatically trigger historical provider downloads;
- change strategy/risk policy based on the new metadata.
