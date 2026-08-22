# Options data-provider decision

**Decision date:** 2026-08-22

This document records the initial data architecture for the U.S. options research engine. It is deliberately revisitable: provider adapters sit behind the point-in-time store so changing vendors does not require rewriting research logic.

## Primary historical research source: Databento

Use Databento as the first historical source for model development and 4x-premium outcome calibration.

Why:

- Databento's `OPRA.PILLAR` dataset provides consolidated U.S. equity-options data and advertises OPRA history back to 2013.
- The schemas needed by this project exist directly: consolidated top-of-book (`cmbp-1` / sampled `cbbo-*`), trades, OHLCV, definitions, and statistics/open interest.
- Parent symbology (`AAPL.OPT`, `SPY.OPT`, etc.) can request an underlying's option family without first enumerating every OCC contract.
- Historical access supports usage-based pricing, which lets research begin with carefully scoped samples before paying to ingest a much larger archive.
- Databento's U.S. equities datasets provide a compatible source for bulk equity bars. `EQUS.SUMMARY` provides consolidated end-of-day OHLCV, while `EQUS.MINI`/other U.S. equity feeds can support intraday work later.
- Receive timestamps are available on market-data schemas, which maps naturally to the store's `available_at` anti-lookahead field.

Initial schemas:

| Purpose | Dataset | Schema |
| --- | --- | --- |
| Whole-market daily chart research | `EQUS.SUMMARY` | `ohlcv-1d` |
| Option target-path/outcome research | `OPRA.PILLAR` | `cbbo-1m` |
| Later higher-resolution option microstructure | `OPRA.PILLAR` | `cmbp-1` |
| Option definitions | `OPRA.PILLAR` | `definition` |
| Open interest / statistics | `OPRA.PILLAR` | `statistics` |

The first adapter intentionally starts with daily equity OHLCV and one-minute consolidated option BBO. Tick-level data should only be purchased/processed when a measured research question requires it; ingesting terabytes because it feels more institutional is not an edge.

Official references:

- https://databento.com/options
- https://databento.com/docs/venues-and-datasets
- https://databento.com/docs/examples/options/equity-options-introduction/using-parent-symbology-to-fetch-an-option-chain
- https://databento.com/docs/schemas-and-data-formats/cbbo
- https://databento.com/docs/venues-and-datasets/equs-summary

## Paper/live integration source: Alpaca

Keep Alpaca as the first paper/live integration candidate because the repository already has Alpaca paper research scripts and Alpaca exposes option chains, snapshots, historical bars/trades, and real-time option streams.

Important limitation for model research: Alpaca currently documents historical options availability only since February 2024. It also distinguishes its official subscription OPRA feed from its free indicative feed; the indicative quotes are not actual OPRA quotes. That is why the initial long-history calibration source is Databento rather than Alpaca alone.

Official references:

- https://docs.alpaca.markets/us/docs/historical-option-data
- https://docs.alpaca.markets/us/docs/real-time-option-data
- https://docs.alpaca.markets/us/v1.4.2/reference/optionchain

## Alternative archive: Massive

Massive is a viable alternate/source-of-truth check. Its U.S. options quote flat files document top-of-book history back to March 2022, and its REST API exposes historical option quotes. The flat-file archive is extremely large, so it is not the first ingestion path for this project.

Official references:

- https://massive.com/docs/flat-files/options/quotes
- https://massive.com/docs/rest/options/quotes

## Vendor-independence rule

Provider responses are normalized into `OptionsResearchStore` before research code consumes them. The chart scanner, options ranking, calibration, and later expected-value model should not import vendor SDKs directly.

That separation gives us four useful controls:

1. We can replay the same point-in-time dataset against multiple model versions.
2. We can compare two providers for data-quality discrepancies without changing the strategy.
3. Backtests cannot silently fetch revised data from the internet while they run.
4. Provider cost, licensing, or API changes do not force a rewrite of the research engine.

## Credential rule

Do not commit provider API keys. The Databento adapter accepts a key at runtime or reads `DATABENTO_API_KEY`; it never persists the credential in the research store.
