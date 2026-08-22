# Vibe-Trading Personal Trading Platform Roadmap

The goal is a finished personal market operating system, not a collection of
independent scanners. The platform must preserve point-in-time research,
fail-closed data/risk gates, paper/live separation, reproducibility and a clear
`NO_TRADE` outcome.

## Product contract

```text
DATA
  -> RESEARCH
  -> EVIDENCE
  -> DECISION
  -> RISK
  -> APPROVAL
  -> EXECUTION
  -> JOURNAL
  -> ATTRIBUTION / LEARNING
```

Every stage must be inspectable and versioned. A later stage may reject an
upstream candidate but may not silently bypass an upstream failure.

## Environment contract

- **research** — historical/current research; no broker mutation.
- **paper** — current market + paper broker; explicit approval before mutation.
- **live** — real account; disabled until separate production-readiness work is
  completed and explicitly enabled through the existing mandate/order safety
  boundary.

The Trading Desk foundation deliberately maps `live` to `live_disabled`.

## Milestone 0 — platform spine (in progress)

- [x] Versioned Trading Desk snapshot contract.
- [x] Durable DuckDB snapshot store.
- [x] Append-only platform audit events.
- [x] Research/paper/live environment identity.
- [x] Explicit execution-mode contract.
- [x] Fail-closed component freshness/data-quality gate.
- [x] Personal continuous-analysis publisher hook.
- [x] Read-only `/api/trading-desk` and `/api/trading-desk/events` endpoints.
- [x] First React Trading Desk screen (`/trading`).
- [x] Targeted backend + frontend CI workflow.
- [ ] Add Trading Desk to primary navigation and consolidate overlapping screens.
- [ ] Add deployment/runtime supervisor and health heartbeat.

## Milestone 1 — production data plane (P0)

- [ ] Daily/intraday whole-market equity ingestion.
- [ ] Corporate actions, splits, delistings and symbol-master lineage.
- [ ] Authoritative market sessions / holidays / early closes.
- [ ] OPRA-quality focused option quotes, IV, Greeks, OI and volume.
- [ ] Option definitions, deliverables and adjustment handling.
- [ ] Provider cross-check and disagreement rules.
- [ ] Bad-tick, crossed-market and stale-quote quarantine.
- [ ] Data gap/recovery metrics visible on Trading Desk.

## Milestone 2 — catalyst + macro intelligence (P0/P1)

- [ ] Timestamped news/catalyst ingestion.
- [ ] SEC filing event extraction.
- [ ] Earnings/guidance calendar and surprise context.
- [ ] FDA/regulatory, M&A, contracts, lawsuits and analyst-event taxonomy.
- [ ] Source-quality, novelty, corroboration and direction scores.
- [ ] Macro event calendar: Fed, CPI, payrolls, Treasury, major auctions.
- [ ] Cross-asset context: SPY/QQQ/IWM, VIX, rates, DXY, oil, credit proxies.

## Milestone 3 — strategy and option intelligence (P1)

- [ ] Multi-timeframe structure: intraday/hour/day/week/month.
- [ ] Relative strength and market/sector breadth.
- [ ] Breakout/trend, post-earnings drift, catalyst momentum and reversal families.
- [ ] IV rank/percentile, skew and term structure.
- [ ] Expected move and realized-vs-implied volatility.
- [ ] Gamma/theta/vega efficiency and IV-crush treatment.
- [ ] Strategy disagreement / ensemble arbiter rather than one monolithic score.

## Milestone 4 — historical evidence engine (P0)

- [x] Point-in-time store and anti-lookahead primitives.
- [x] Option-path outcome labels.
- [x] Empirical EV gate.
- [x] Embargoed walk-forward evaluation.
- [x] Adaptive +100/+200/+300 payoff-policy comparison.
- [ ] Production replay runner across years and full candidate history.
- [ ] Survivorship-bias-safe universe reconstruction.
- [ ] Slippage/spread/latency models by liquidity bucket.
- [ ] Regime-conditioned EV and confidence intervals.
- [ ] Model/strategy/version lineage on every replay result.

## Milestone 5 — portfolio and risk brain (P0)

- [x] Per-trade and total premium-risk limits.
- [x] Same-underlying/expiry/theme concentration limits.
- [ ] Account state normalization across supported brokers.
- [ ] Equity beta, sector and factor concentration.
- [ ] Portfolio delta/gamma/theta/vega aggregation.
- [ ] Correlation clusters and hidden duplicate bets.
- [ ] Daily/weekly loss and drawdown kill thresholds.
- [ ] Event-risk concentration and overnight-risk policies.
- [ ] Portfolio-level scenario/stress tests.

## Milestone 6 — execution engine (paper first) (P0/P1)

- [x] Alpaca paper-only options order seam.
- [x] Limit-order / spread / premium-risk checks.
- [ ] Fresh broker market clock rather than caller-provided market-open flag.
- [ ] Execution-grade option snapshot immediately before order approval.
- [ ] Entry-zone and do-not-chase rules.
- [ ] Order state machine: proposed -> approved -> submitted -> partial -> filled.
- [ ] Cancel/replace policy with bounded retries.
- [ ] Exit engine: profit, invalidation, theta/time, catalyst and risk exits.
- [ ] Fill/slippage attribution.
- [ ] Shadow portfolio before autonomous paper execution.

## Milestone 7 — journal, attribution and learning (P1/P2)

- [ ] Journal every surfaced, rejected and executed candidate.
- [ ] Persist reasons for every gate pass/fail.
- [ ] Missed-opportunity analysis for rejected candidates.
- [ ] Counterfactual contract/expiry/entry/exit analysis.
- [ ] Win/loss attribution: stock selection vs contract vs timing vs regime.
- [ ] Strategy tournament on later unseen data.
- [ ] Promote/retire strategies only through explicit versioned policy changes.
- [ ] No uncontrolled online self-modification of live decision rules.

## Milestone 8 — finished control room (P1)

- [x] Initial Trading Desk page.
- [ ] Whole-market funnel with live stage counts.
- [ ] Market regime / breadth / macro header.
- [ ] Top opportunities with evidence for and against.
- [ ] Portfolio Greeks, risk budgets and concentration map.
- [ ] Upcoming catalyst/event calendar.
- [ ] Data/provider health panel.
- [ ] Audit/journal timeline.
- [ ] Paper approval panel.
- [ ] Natural-language commands backed by the same platform state.
- [ ] Notifications only for meaningful state changes.

## Finished-platform definition

The platform is not "finished" because it has many indicators. It is finished
when one supervised runtime can:

1. know whether its data is trustworthy;
2. scan the entire intended U.S. universe;
3. deeply analyze only the best candidates;
4. produce calibrated evidence or explicitly state that evidence is missing;
5. apply portfolio/account risk;
6. show `TRADE_READY_RESEARCH`, `WATCH`, `PASS`, or `NO_TRADE` with reasons;
7. paper-execute only after the separate approval/safety boundary;
8. track fills and exits;
9. journal every decision and rejected opportunity; and
10. replay the same versioned logic historically without lookahead.

Live-money automation is a separate readiness milestone, not a side effect of
finishing the research platform.
