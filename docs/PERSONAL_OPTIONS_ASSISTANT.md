# Personal U.S. Options Research Assistant

This layer keeps the broad-market discipline of the Vibe-Trading research stack while making the operating model appropriate for one personal account.

It is deliberately **not** an HFT system and does not need institutional infrastructure. The expensive work is concentrated on a small focus set after a whole-market screen.

## Decision funnel

```text
U.S. listed universe
  -> point-in-time chart/liquidity screen
  -> ~200 focus names
  -> ~50 deep option/catalyst names
  -> option quality + IV/Greeks
  -> empirical EV + walk-forward evidence
  -> market-regime fit
  -> personal-account premium-risk gate
  -> 0-2 TRADE_READY_RESEARCH candidates, WATCH, or NO_TRADE
```

`TRADE_READY_RESEARCH` is intentionally not an order instruction. Broker execution remains separate, and PR #7's Alpaca paper path still defaults to dry-run and paper-only safeguards.

## New modules

- `regime.py` — broad benchmark trend/volatility/breadth regime context.
- `volatility.py` — contract spread, OI/volume, delta, theta, IV percentile and IV-vs-realized-vol diagnostics.
- `personal.py` — combines ranking, option quality, empirical evidence, regime and portfolio risk into `TRADE_READY_RESEARCH`, `WATCH`, or `PASS`.
- `alpaca_current.py` — read-only current Alpaca contract/snapshot reader using active contract metadata, current bid/ask, IV and Greeks. The output explicitly marks `indicative` vs `opra`; indicative data is not silently treated as execution-grade.
- `replay.py` — point-in-time historical selection plus separately invoked future outcome labeling. Selection is constrained to `available_at <= research_time`.
- `payoff.py` — compares historical payoff policies instead of forcing every setup to target +300%. Default targets are +100%, +200%, +300%.
- `dashboard.py` — compact personal control-room payload plus meaningful-change alert envelope.
- `personal_service.py` — composes the continuous whole-market analyzer with account state, evidence, personal decisions, dashboard and alerts without any broker writes.

## CLI tools

### Personal dashboard from a continuous-analysis result

```bash
python scripts/run_personal_options_assistant.py \
  --analysis-json data/options-analysis-latest.json \
  --account-equity 50000 \
  --assume-no-open-risk \
  --ev-json data/current-ev.json \
  --walkforward-json data/current-walkforward.json \
  --regime-json data/current-regime.json \
  --output data/personal-options-latest.json
```

For a configured Alpaca account, `--alpaca-account` can replace `--account-equity`. That path is read-only and uses account equity plus recognizable long OCC option positions for the premium-risk gate. It does not submit an order.

### Point-in-time replay

```bash
python scripts/replay_options_research.py \
  --store data/options.duckdb \
  --symbols data/us_symbols.json \
  --times data/replay_times.json \
  --evaluation-as-of 2026-08-22T23:59:00-04:00 \
  --output data/replay-results.json
```

Future quotes are used only in the outcome-label stage. They must never be joined back into the feature set used for selection.

### Payoff-policy selection

```bash
python scripts/evaluate_option_payoff_policy.py \
  --outcomes-json data/replay-outcomes.json \
  --targets 100,200,300
```

The selected target is the one with the strongest conservative historical evidence among policies that pass the configured sample/EV lower-bound gates. If none pass, the output is `NO_PAYOFF_POLICY`.

## Personal-account defaults

The existing portfolio-risk layer remains conservative by default: 1% maximum premium risk per candidate, 5% total open premium risk, concentration limits for the same underlying/expiry/theme, positive empirical EV required, and walk-forward evidence required. The personal decision layer additionally caps trade-ready candidates and defaults to one contract per trade unless configuration is changed.

These are software defaults for research/paper testing, not a guarantee that the percentages are appropriate for every person or market environment.

## What still needs live data to become production-complete

The code paths are now present, but real usefulness depends on keeping these inputs fresh:

1. point-in-time U.S. equity bars for the whole universe;
2. current focused option chains with reliable bid/ask, IV, Greeks, OI and preferably current volume;
3. catalyst/news events with publication timestamps;
4. historical option paths for replay/outcome labels;
5. an authoritative market calendar; and
6. periodically refreshed empirical EV/walk-forward reports.

For current Alpaca option data, the official OPRA feed should be preferred before any execution decision. The free `indicative` feed is explicitly labeled and should be treated as research-quality rather than execution-quality.

## Design principle

The system should never manufacture a trade because the user asked for one. If evidence, data quality, volatility economics, regime, or risk gates fail, the correct output is `NO_TRADE` or `WATCH`.
