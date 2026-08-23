# Historical IV / Greeks Evidence

Historical implied volatility and Greeks are modeled as a separate evidence source from OPRA quotes, definitions, open interest and volume.

That separation is intentional. Exchange quote/reference data and calculated/vendor volatility analytics have different provenance, timing and model assumptions. A later IV calculation must never silently rewrite an earlier exchange quote.

## Canonical volatility store

`HistoricalOptionVolatilityStore` is append-only and stores:

- OCC/OSI contract symbol
- project underlying
- `event_ts`
- `available_at`
- implied volatility, canonicalized as a fraction (`0.40` = 40%)
- optional delta / gamma / theta / vega / rho
- optional underlying price
- optional risk-free rate and dividend yield
- optional model label
- optional vendor observation ID
- source provenance

Later revisions of the same contract/event timestamp are preserved. An as-of query sees only revisions whose `available_at <= as_of`.

## Why IV is not written into `option_quotes`

The raw historical quote remains the exchange-market observation. IV/Greeks may come from:

- a licensed historical analytics vendor;
- a future direct provider adapter;
- a separately reviewed in-house pricing model.

Those sources may use different rates, dividends, early-exercise assumptions, interpolation and corporate-action handling. Keeping them separate allows apples-to-apples model comparison and prevents a calculated field from masquerading as raw OPRA data.

## Event-time alignment

`HistoricalVolatilityResearchStoreView` attaches volatility evidence to each quote using a backward event-time join by contract.

A volatility observation may enrich a quote only when:

1. the volatility observation was knowable by the research `as_of` timestamp; and
2. its `event_ts` is at or before that individual quote's `event_ts`.

The lookup intentionally reaches before the quote lookback window. For example, the prior day's EOD IV may be the latest legitimate volatility observation for a morning quote. A later intraday IV observation cannot leak backward onto an earlier quote.

Quote-native IV wins when already present. The overlay only fills missing values. Missing native Greeks may be filled by the aligned evidence source.

## Full historical evidence order

`historical_evidence_store_view(...)` composes the research inputs in this order:

```text
raw OptionsResearchStore quote
  -> point-in-time definitions / OI / completed daily volume
  -> point-in-time IV / Greeks
  -> existing replay / experiment engine
```

No chart, option-selection, EV, walk-forward or payoff rule is copied into the evidence layer.

## Local vendor-neutral import

`scripts/import_historical_option_volatility.py` imports licensed/local CSV or JSON observations.

The command performs no provider network request. It supports:

- `--iv-unit fraction`
- `--iv-unit percent` (explicitly converts e.g. `40.0` to `0.40`)
- `--source` provenance override
- optional `--model`
- `--plan-only`

Source provenance is mandatory. A plan-only run does not create/open the volatility store.

## Full-evidence experiment CLI

`scripts/run_options_historical_experiment_evidence.py` reuses the existing historical experiment CLI and adds only:

```text
--volatility-store <path>
```

It preserves the base command's:

- authoritative XNYS schedule
- survivorship-bias controls
- fixed-horizon outcomes
- experiment/data lineage
- `--plan-only` behavior

Plan-only opens neither the research store nor the volatility store. A real run fails closed when the volatility store path does not exist.

## Model and vendor discipline

This layer deliberately does not choose a universal IV model for all U.S. equity options.

U.S. single-stock options are generally American-style and can be affected by dividends, rates, early exercise and corporate-action deliverables. A simplistic zero-rate Black-Scholes inversion is not acceptable as an invisible fallback for production evidence.

A future direct vendor adapter or in-house model should preserve:

- source/model version
- underlying price used
- risk-free rate / curve reference
- dividend assumption
- calculation timestamp and availability timestamp
- corporate-action/adjustment treatment

so its results can be evaluated separately.

## Safety

Historical volatility evidence is research-only. This layer:

- performs no broker mutations
- does not enable live trading
- does not alter strategy rules automatically
- does not download paid data automatically
- does not store credentials

The presence of historical IV/Greeks improves replay completeness; it does not make historical hit rates guaranteed future probabilities.
