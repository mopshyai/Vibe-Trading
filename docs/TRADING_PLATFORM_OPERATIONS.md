# Trading Platform Operations — Supervised PAPER

This runbook is the operational path from an empty deployment to a continuously
refreshing personal Trading Desk. The platform is intentionally fail-closed: no
historical evidence, stale data, blocked account risk, closed market, non-OPRA
execution data, or a non-paper broker profile can become a submitted order.

## What runs continuously

`python scripts/start_trading_platform.py` supervises both:

- the Vibe-Trading API/frontend (`/trading`, `/api/trading-desk`), and
- `scripts/run_trading_platform_worker.py`.

The worker refreshes the market calendar/universe, runs the market scan, refreshes
focused Alpaca options/news, rebuilds candidate evidence from existing historical
outcomes, reads PAPER account risk, publishes the Trading Desk, prepares a dry-run
paper proposal, reconciles previously submitted paper orders, and writes a
preflight/heartbeat.

It does **not** submit a new order and does **not** automatically download paid
historical data.

## Runtime configuration

Use runtime/hosting secrets, never committed files:

```text
ALPACA_PROFILE=paper
APCA_API_KEY_ID=<paper key id>
APCA_API_SECRET_KEY=<paper secret>
DATABENTO_API_KEY=<historical provider key>
TRADING_PLATFORM_OPTION_FEED=opra
TRADING_PLATFORM_DATA_DIR=/app/data
```

The first two Alpaca secret values may also be supplied through the configured
TAP credential path. `DATABENTO_API_KEY` is needed for historical backfill, not
for a read-only current Alpaca cycle.

## 1. Preflight

Run:

```bash
python scripts/trading_platform_preflight.py --store /app/data/trading-platform.duckdb
```

The report separates:

- `platform_operational`
- `research_data_ready`
- `historical_replay_ready`
- `paper_runtime_ready`
- `paper_submit_ready_now`

The report contains no secret values. Missing setup is returned under
`operator_actions`.

## 2. Inspect historical backfill before any provider request

Databento historical requests can incur charges. Inspect the plan first:

```bash
python scripts/backfill_us_market_data.py \
  --store /app/data/options-research.duckdb \
  --symbols-file /app/data/us-universe.json \
  --start 2024-01-01 \
  --end 2026-08-21 \
  --equities \
  --options \
  --plan-only
```

Do not remove `--plan-only` until the requested provider scope/cost is accepted.
The backfill command reads `DATABENTO_API_KEY` from the environment; there is no
CLI API-key argument.

## 3. Populate the point-in-time research store

After the backfill scope is approved, run the same command without
`--plan-only`. This changes only the research DuckDB; it does not place a broker
order and does not mark historical rows as current market data.

For large OPRA archives, split the requested date/symbol windows instead of
assuming one giant synchronous request is appropriate.

## 4. Build historical outcomes offline

Once the PIT store contains equity and option history:

```bash
python scripts/build_historical_option_outcomes.py \
  --store /app/data/options-research.duckdb \
  --symbols-file /app/data/us-universe.json \
  --start 2024-01-01 \
  --end 2026-08-21 \
  --sample-every-sessions 5 \
  --output /app/data/historical-outcomes.json
```

This command makes no provider or broker request. Selection uses only rows whose
`available_at` was known at each research timestamp. Outcome labeling is a later,
separate step inside replay.

## 5. Candidate EV / walk-forward evidence

The continuous worker performs this automatically each cycle when
`historical-outcomes.json` exists. It can also be run directly:

```bash
python scripts/build_options_evidence.py \
  --outcomes-json /app/data/historical-outcomes.json \
  --analysis-json /app/data/analysis.json \
  --ev-output /app/data/ev-reports.json \
  --walkforward-output /app/data/walkforward-reports.json \
  --summary-output /app/data/evidence-summary.json
```

Evidence is fail-closed:

- outcome labels with `evaluation_as_of` later than the evidence timestamp are excluded;
- calls and puts use separate history pools;
- conservative empirical EV must pass;
- embargoed walk-forward evaluation must pass; and
- the current candidate score must clear the final threshold trained on eligible history.

## 6. Continuous worker

For a one-cycle operational test:

```bash
python scripts/run_trading_platform_worker.py --data-dir /app/data --once --force-full
```

For continuous operation:

```bash
python scripts/run_trading_platform_worker.py --data-dir /app/data --force-full
```

The worker never reuses old EV/walk-forward approvals after an evidence-build
failure, and never treats an unavailable current account-risk read as safe.

## 7. Paper proposal

When a candidate is `TRADE_READY_RESEARCH`, the worker prepares a current dry-run
proposal using the supervised paper lifecycle. It rechecks:

- Alpaca PAPER profile,
- broker market clock,
- exact-contract current quote,
- quote age,
- OPRA for submission-grade data,
- spread/debit limits,
- empirical EV,
- walk-forward evidence, and
- portfolio/account risk.

No paper order is submitted by the worker.

## 8. Explicit PAPER submit

A new paper broker mutation requires both switches:

```bash
python scripts/run_paper_option_lifecycle.py \
  --input-json /app/data/paper-execution-input.json \
  --store /app/data/trading-platform.duckdb \
  --option-feed opra \
  --submit-paper \
  --confirm-paper-submit \
  --output /app/data/paper-submit.json
```

The submit seam accepts only the Alpaca PAPER host/profile, long call/put BUY,
integer contracts, LIMIT + DAY, and current passing gates. It posts directly to
the PAPER REST endpoint (or TAP) and does not require `alpaca-py` in production.

## 9. Reconcile fills

The continuous worker runs this read-only sync automatically. Manual equivalent:

```bash
python scripts/run_paper_option_lifecycle.py \
  --sync-only \
  --store /app/data/trading-platform.duckdb
```

Lifecycle states are append-only: proposed → submitted → partial fill / filled /
cancelled / expired / error. Repeated sync of the same terminal state is
idempotent.

## 10. Render deployment

`render.yaml` defines one Docker web service because the API and continuous
worker currently share DuckDB on one persistent disk. The blueprint uses:

- service: `vibe-trading-paper`
- one instance
- persistent `/app/data`
- `python scripts/start_trading_platform.py`
- `/live` health check
- `ALPACA_PROFILE=paper`
- OPRA worker mode
- secret values marked `sync: false`

The persistent-disk service is a paid Render resource. Creating it and entering
the three secret values is an operator action; no key belongs in Git or chat.

## Hard boundaries

- No LIVE order path is enabled by the supervised paper lifecycle.
- No automatic new paper submission is performed by the continuous worker.
- No historical provider spend occurs automatically.
- Missing/stale data and missing evidence produce WATCH / NO TRADE, not a forced setup.
- A +300% target means a 4x option premium scenario, not a promised or expected return.
