# Metadata-Aware Historical Replay

The raw historical option quote table remains immutable. Metadata-aware replay is a read-only view over the same point-in-time store.

## Why a view instead of rewriting quotes

A quote observed at 15:30 ET must not magically acquire the final daily volume that was only knowable after the session close. Likewise, a later definition correction must not overwrite what the research system knew earlier.

`MetadataAwareResearchStoreView` delegates equity reads unchanged and routes option-quote reads through `option_quotes_with_metadata_asof(...)`.

That enrichment obeys the same `available_at <= as_of` boundary:

- quote-native open interest / volume wins when present;
- otherwise the latest published open-interest or completed daily-volume statistic may fill the missing field;
- definitions are attached only when already available;
- no raw quote row is changed.

## Reusing the existing replay engine

The metadata-aware layer deliberately does not copy strategy logic.

Use:

- `replay_selection_with_metadata_at(...)` for one historical timestamp;
- `run_historical_experiment_with_metadata(...)` for the multi-session experiment runner.

Both call the existing replay/experiment engines through the metadata-aware store view. This keeps the chart ranking, option feasibility, fixed-horizon outcomes, universe controls and anti-lookahead rules identical to the base replay.

## CLI

`scripts/run_options_historical_experiment_metadata.py` is a thin wrapper around the existing `run_options_historical_experiment.py` command.

It reuses the base command's:

- arguments;
- authoritative XNYS schedule;
- early-close handling;
- point-in-time/static universe policy;
- `--plan-only` behavior;
- output formatting;
- experiment lineage.

The only substitution is the metadata-aware experiment function.

Plan-only still creates no store, makes no provider request and performs no broker operation.

## What metadata improves

When historical records exist at the research timestamp, replay can use:

- contract definitions;
- published open interest;
- completed-session daily volume when already available.

This improves historical liquidity and contract-reference realism.

## What metadata does not solve

Historical implied volatility and Greeks are still not fabricated.

If a historical quote has no IV, the existing replay scorer rejects that contract and emits `historical_implied_volatility_missing`. The metadata-aware view does not fill, estimate or suppress that warning.

Therefore a CBBO + definition + OI + daily-volume archive remains incomplete for production replay until a defensible point-in-time IV/Greeks source or separately reviewed pricing model is available.

## Safety

Metadata-aware replay is historical research only. It adds no broker calls, order writes, credential changes, deployments or automatic strategy-policy changes.
