import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Activity,
  AlertTriangle,
  BrainCircuit,
  Clock3,
  Database,
  Gauge,
  RefreshCw,
  ShieldCheck,
  TrendingUp,
  WalletCards,
  Zap,
} from "lucide-react";
import {
  tradingDeskApi,
  type DeskDecision,
  type TradingDeskAttribution,
  type TradingDeskEvent,
  type TradingDeskOpportunity,
  type TradingDeskSnapshot,
} from "@/lib/tradingDesk";

function fmtNumber(value: number | null | undefined, digits = 0) {
  if (value == null || !Number.isFinite(value)) return "—";
  return value.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function fmtPct(value: number | null | undefined, digits = 1) {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${value.toFixed(digits)}%`;
}

function fmtMoney(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return "—";
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(value);
}

function fmtRatio(value: number | null | undefined, digits = 2) {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${value.toFixed(digits)}×`;
}

function decisionClass(decision: DeskDecision) {
  if (decision === "TRADE_READY_RESEARCH") return "border-emerald-500/30 bg-emerald-500/10 text-emerald-500";
  if (decision === "WATCH") return "border-amber-500/30 bg-amber-500/10 text-amber-500";
  if (decision === "PASS" || decision === "NO_TRADE") return "border-border bg-muted/50 text-muted-foreground";
  return "border-border bg-muted/30 text-muted-foreground";
}

function DecisionBadge({ decision }: { decision: DeskDecision }) {
  return (
    <span className={`inline-flex rounded-full border px-2.5 py-1 text-[11px] font-semibold tracking-wide ${decisionClass(decision)}`}>
      {decision.replaceAll("_", " ")}
    </span>
  );
}

function MetricCard({ label, value, detail, icon: Icon }: { label: string; value: string; detail?: string; icon: typeof Activity }) {
  return (
    <div className="rounded-xl border border-border/70 bg-card p-4">
      <div className="flex items-center justify-between gap-3">
        <span className="text-xs font-medium text-muted-foreground">{label}</span>
        <Icon className="h-4 w-4 text-muted-foreground" />
      </div>
      <div className="mt-2 text-2xl font-semibold tracking-tight">{value}</div>
      {detail && <div className="mt-1 text-[11px] text-muted-foreground">{detail}</div>}
    </div>
  );
}

function Opportunity({ item, index }: { item: TradingDeskOpportunity; index: number }) {
  const reasons = item.decision === "PASS" ? item.hard_reasons : item.watch_reasons;
  const hasSurface = [
    item.surface_efficiency_score,
    item.surface_required_move_ratio,
    item.surface_atm_expected_move_pct,
    item.candidate_iv_premium_to_atm_points,
  ].some((value) => value != null)
    || Boolean(item.surface_term_structure_state || item.surface_skew_state || item.surface_implied_vs_realized_state);

  return (
    <div className="rounded-xl border border-border/70 bg-card p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-xs font-semibold text-muted-foreground">#{index + 1}</span>
            <h3 className="text-lg font-semibold">{item.symbol}</h3>
            <DecisionBadge decision={item.decision} />
          </div>
          <div className="mt-1 font-mono text-xs text-muted-foreground">
            {item.contract_symbol || "Underlying research candidate"}
          </div>
        </div>
        <div className="text-right">
          <div className="text-xs text-muted-foreground">Composite</div>
          <div className="text-xl font-semibold">{fmtNumber(item.composite_score, 1)}</div>
        </div>
      </div>

      <div className="mt-4 grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-7">
        <Small label="Rank" value={fmtNumber(item.ranking_score, 1)} />
        <Small label="Option" value={fmtNumber(item.option_quality_score, 1)} />
        <Small label="Regime" value={fmtNumber(item.regime_fit_score, 1)} />
        <Small label="Expected return" value={fmtPct(item.expected_return_pct)} />
        <Small label="Lower bound" value={fmtPct(item.lower_confidence_bound_pct)} />
        <Small label="Entry ask" value={item.entry_ask == null ? "—" : `$${item.entry_ask.toFixed(2)}`} />
        <Small label="Max loss" value={fmtMoney(item.max_loss_usd_per_contract)} />
      </div>

      <div className="mt-4 grid gap-3 md:grid-cols-3">
        <Small label="Historical samples" value={fmtNumber(item.historical_samples)} />
        <Small label="Target hit rate" value={item.historical_target_hit_rate == null ? "—" : fmtPct(item.historical_target_hit_rate * 100)} />
        <Small label="Spread / IV pctile" value={`${fmtPct(item.spread_pct)} / ${fmtNumber(item.iv_percentile, 0)}`} />
      </div>

      {hasSurface && (
        <div className="mt-4 rounded-lg border border-border/60 bg-muted/20 p-3">
          <div className="mb-2 flex items-center justify-between gap-2">
            <div className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">Volatility surface</div>
            <div className="text-[10px] text-muted-foreground">relative contract context · not probability</div>
          </div>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-7">
            <Small label="Efficiency" value={fmtNumber(item.surface_efficiency_score, 1)} />
            <Small label="Move / ATM exp." value={fmtRatio(item.surface_required_move_ratio)} />
            <Small label="ATM exp. move" value={fmtPct(item.surface_atm_expected_move_pct)} />
            <Small
              label="IV vs ATM"
              value={item.candidate_iv_premium_to_atm_points == null ? "—" : `${item.candidate_iv_premium_to_atm_points >= 0 ? "+" : ""}${item.candidate_iv_premium_to_atm_points.toFixed(1)} vol pts`}
            />
            <Small label="Term" value={item.surface_term_structure_state || "—"} />
            <Small label="Skew" value={item.surface_skew_state || "—"} />
            <Small label="IV vs RV" value={item.surface_implied_vs_realized_state || "—"} />
          </div>
        </div>
      )}

      {reasons.length > 0 && (
        <div className="mt-4 rounded-lg bg-muted/50 px-3 py-2 text-xs text-muted-foreground">
          {reasons.join(" · ")}
        </div>
      )}
    </div>
  );
}

function Small({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="mt-0.5 text-sm font-medium">{value}</div>
    </div>
  );
}

export function TradingDesk() {
  const [snapshot, setSnapshot] = useState<TradingDeskSnapshot | null>(null);
  const [events, setEvents] = useState<TradingDeskEvent[]>([]);
  const [attribution, setAttribution] = useState<TradingDeskAttribution | null>(null);
  const [counts, setCounts] = useState({ snapshots: 0, events: 0 });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [desk, eventResult, attributionResult] = await Promise.all([
        tradingDeskApi.getLatest(),
        tradingDeskApi.getEvents(25),
        tradingDeskApi.getAttribution(),
      ]);
      setSnapshot(desk.snapshot);
      setCounts(desk.counts);
      setEvents(eventResult.events);
      setAttribution(attributionResult.attribution);
      setError(null);
      setLastRefresh(new Date());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 15_000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const dataStatus = useMemo(() => {
    if (!snapshot) return "NO DATA";
    return snapshot.data_quality.healthy ? "HEALTHY" : "DEGRADED";
  }, [snapshot]);

  if (loading) {
    return <div className="flex h-full items-center justify-center text-sm text-muted-foreground">Loading Trading Desk…</div>;
  }

  return (
    <main className="h-full overflow-auto bg-background" aria-label="Trading Desk">
      <div className="mx-auto max-w-[1600px] space-y-5 p-4 md:p-6">
        <header className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <div className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
              <BrainCircuit className="h-4 w-4" />
              Personal Market Operating System
            </div>
            <h1 className="mt-1 text-2xl font-semibold tracking-tight">Trading Desk</h1>
            <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
              Whole-market research → volatility surface → evidence → risk → paper approval → retrospective learning. Live execution remains disabled.
            </p>
          </div>
          <div className="flex items-center gap-2">
            {snapshot && <DecisionBadge decision={snapshot.decision} />}
            <button
              onClick={() => void refresh()}
              className="inline-flex items-center gap-2 rounded-md border border-border bg-card px-3 py-2 text-xs font-medium hover:bg-muted"
            >
              <RefreshCw className="h-3.5 w-3.5" /> Refresh
            </button>
          </div>
        </header>

        {error && (
          <div className="flex items-center gap-2 rounded-lg border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive">
            <AlertTriangle className="h-4 w-4" /> {error}
          </div>
        )}

        {!snapshot ? (
          <section className="rounded-xl border border-dashed border-border p-10 text-center">
            <Database className="mx-auto h-8 w-8 text-muted-foreground" />
            <h2 className="mt-3 text-lg font-semibold">Trading platform store is ready</h2>
            <p className="mx-auto mt-1 max-w-xl text-sm text-muted-foreground">
              No analysis cycle has been published yet. Once the continuous personal scanner publishes its first cycle, this desk will populate automatically.
            </p>
          </section>
        ) : (
          <>
            <section className="rounded-xl border border-border/70 bg-card p-5">
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div>
                  <div className="text-xs uppercase tracking-wide text-muted-foreground">Current decision</div>
                  <div className="mt-1 text-xl font-semibold">{snapshot.headline}</div>
                  <div className="mt-2 flex flex-wrap gap-3 text-xs text-muted-foreground">
                    <span>Phase: <strong className="text-foreground">{snapshot.market_phase}</strong></span>
                    <span>Regime: <strong className="text-foreground">{snapshot.market_regime}</strong></span>
                    <span>Environment: <strong className="text-foreground">{snapshot.environment}</strong></span>
                    <span>Execution: <strong className="text-foreground">{snapshot.execution_mode}</strong></span>
                  </div>
                </div>
                <div className="text-right text-xs text-muted-foreground">
                  <div>Snapshot {snapshot.snapshot_id.slice(0, 10)}</div>
                  <div>{new Date(snapshot.created_at).toLocaleString()}</div>
                </div>
              </div>
            </section>

            <section className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-6">
              <MetricCard label="Universe" value={fmtNumber(snapshot.funnel.universe)} detail="U.S. securities received" icon={Database} />
              <MetricCard label="Chart candidates" value={fmtNumber(snapshot.funnel.chart_candidates)} detail={`${fmtNumber(snapshot.funnel.chart_eligible)} eligible`} icon={TrendingUp} />
              <MetricCard label="Deep analyzed" value={fmtNumber(snapshot.funnel.deep_analyzed)} detail="Options + catalysts" icon={BrainCircuit} />
              <MetricCard label="Trade ready" value={fmtNumber(snapshot.funnel.trade_ready)} detail="After all configured gates" icon={Zap} />
              <MetricCard label="Data" value={dataStatus} detail={`${snapshot.data_quality.blocking_reasons.length} blocking issues`} icon={Gauge} />
              <MetricCard label="Account risk" value={snapshot.risk.open_premium_risk_pct == null ? "—" : fmtPct(snapshot.risk.open_premium_risk_pct)} detail={`${snapshot.risk.positions} open positions`} icon={WalletCards} />
            </section>

            <section className="grid gap-5 xl:grid-cols-[minmax(0,2fr)_minmax(320px,1fr)]">
              <div className="space-y-3">
                <div className="flex items-center justify-between">
                  <h2 className="text-sm font-semibold">Top opportunities</h2>
                  <span className="text-xs text-muted-foreground">{snapshot.opportunities.length} displayed</span>
                </div>
                {snapshot.opportunities.length === 0 ? (
                  <div className="rounded-xl border border-border/70 bg-card p-8 text-center text-sm text-muted-foreground">
                    NO TRADE — no candidate is currently surfaced by the platform.
                  </div>
                ) : snapshot.opportunities.map((item, index) => (
                  <Opportunity key={item.contract_symbol || `${item.symbol}-${index}`} item={item} index={index} />
                ))}
              </div>

              <div className="space-y-5">
                <section className="rounded-xl border border-border/70 bg-card p-4">
                  <div className="flex items-center gap-2">
                    <ShieldCheck className="h-4 w-4" />
                    <h2 className="text-sm font-semibold">System health</h2>
                  </div>
                  <div className="mt-3 space-y-2">
                    {snapshot.data_quality.components.length === 0 ? (
                      <p className="text-xs text-muted-foreground">Health observations have not been published yet.</p>
                    ) : snapshot.data_quality.components.map((item) => (
                      <div key={item.name} className="flex items-center justify-between gap-3 rounded-md bg-muted/40 px-3 py-2 text-xs">
                        <div>
                          <div className="font-medium">{item.name}</div>
                          <div className="text-muted-foreground">{item.source || item.detail || "no source metadata"}</div>
                        </div>
                        <span className={item.status === "ok" ? "text-emerald-500" : item.status === "stale" ? "text-amber-500" : "text-destructive"}>
                          {item.status.toUpperCase()}
                        </span>
                      </div>
                    ))}
                  </div>
                </section>

                <section className="rounded-xl border border-border/70 bg-card p-4">
                  <div className="flex items-center gap-2">
                    <BrainCircuit className="h-4 w-4" />
                    <h2 className="text-sm font-semibold">Learning loop</h2>
                  </div>
                  {!attribution || attribution.samples === 0 ? (
                    <p className="mt-3 text-xs text-muted-foreground">No mature retrospective outcomes yet. Decisions remain uncalibrated until later option paths are observed.</p>
                  ) : (
                    <>
                      <div className="mt-3 grid grid-cols-2 gap-3">
                        <Small label="Mature samples" value={fmtNumber(attribution.samples)} />
                        <Small label="2× touched" value={fmtPct((attribution.overall?.touch_2x_rate || 0) * 100)} />
                        <Small label="4× target hit" value={fmtPct((attribution.overall?.target_hit_rate || 0) * 100)} />
                        <Small label="Full-loss proxy" value={fmtPct((attribution.overall?.full_loss_proxy_rate || 0) * 100)} />
                        <Small label="Missed winners" value={fmtNumber(attribution.missed_opportunities)} />
                        <Small label="Avoided losses" value={fmtNumber(attribution.avoided_losses)} />
                      </div>
                      <div className="mt-3 rounded-md bg-muted/40 px-3 py-2 text-[11px] text-muted-foreground">
                        {attribution.samples < 30
                          ? "Calibration immature: descriptive evidence only; do not promote surface rules from this sample."
                          : "Retrospective evidence only. Policy changes still require explicit versioned review."}
                      </div>
                    </>
                  )}
                </section>

                <section className="rounded-xl border border-border/70 bg-card p-4">
                  <div className="flex items-center gap-2">
                    <Activity className="h-4 w-4" />
                    <h2 className="text-sm font-semibold">Audit stream</h2>
                  </div>
                  <div className="mt-3 space-y-2">
                    {events.length === 0 ? (
                      <p className="text-xs text-muted-foreground">No platform events yet.</p>
                    ) : events.slice(0, 10).map((event) => (
                      <div key={event.event_id} className="border-l border-border pl-3 text-xs">
                        <div className="font-medium">{event.event_type.replaceAll("_", " ")}</div>
                        <div className="mt-0.5 flex items-center gap-1 text-[10px] text-muted-foreground">
                          <Clock3 className="h-3 w-3" /> {new Date(event.occurred_at).toLocaleString()}
                        </div>
                      </div>
                    ))}
                  </div>
                </section>
              </div>
            </section>
          </>
        )}

        <footer className="flex flex-wrap items-center justify-between gap-2 border-t border-border/60 pt-3 text-[10px] text-muted-foreground">
          <span>{snapshot?.system.platform_version || "personal-trading-platform-v0.1"}</span>
          <span>{counts.snapshots} snapshots · {counts.events} audit events · refreshed {lastRefresh ? lastRefresh.toLocaleTimeString() : "—"}</span>
        </footer>
      </div>
    </main>
  );
}
