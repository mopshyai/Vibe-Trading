import { authHeaders } from "@/lib/apiAuth";

export type DeskDecision =
  | "TRADE_READY_RESEARCH"
  | "WATCH"
  | "PASS"
  | "NO_TRADE"
  | "NO_DATA";

export interface TradingDeskHealthComponent {
  name: string;
  status: "ok" | "stale" | "error" | "unknown";
  observed_at?: string | null;
  age_seconds?: number | null;
  source?: string | null;
  detail?: string | null;
  blocking: boolean;
}

export interface TradingDeskOpportunity {
  symbol: string;
  contract_symbol?: string | null;
  decision: DeskDecision;
  direction?: string | null;
  composite_score?: number | null;
  ranking_score?: number | null;
  option_quality_score?: number | null;
  regime_fit_score?: number | null;
  expected_return_pct?: number | null;
  lower_confidence_bound_pct?: number | null;
  historical_samples?: number | null;
  historical_target_hit_rate?: number | null;
  entry_ask?: number | null;
  max_loss_usd_per_contract?: number | null;
  configured_contract_cap?: number | null;
  spread_pct?: number | null;
  iv_percentile?: number | null;
  data_feed?: string | null;
  hard_reasons: string[];
  watch_reasons: string[];
}

export interface RiskBucket {
  key: string;
  risk_usd: number;
  risk_pct: number;
}

export interface TradingDeskRiskSummary {
  account_equity_usd?: number | null;
  open_premium_risk_usd?: number | null;
  open_premium_risk_pct?: number | null;
  daily_realized_pnl_usd?: number | null;
  weekly_realized_pnl_usd?: number | null;
  max_drawdown_pct?: number | null;
  positions: number;
  trading_blocked: boolean;
  blocking_reasons: string[];
  greeks: {
    delta_shares?: number | null;
    dollar_delta_usd?: number | null;
    gamma_scaled?: number | null;
    theta_scaled_per_day?: number | null;
    vega_scaled?: number | null;
    dollar_delta_pct_equity?: number | null;
    coverage?: Record<string, { known: number; positions: number }>;
    [key: string]: unknown;
  };
  concentration: {
    underlying?: RiskBucket[];
    sector?: RiskBucket[];
    expiry?: RiskBucket[];
    correlation?: {
      threshold?: number;
      underlyings_with_history?: number;
      aligned_observations?: number;
      clusters?: Array<{ underlyings: string[]; risk_usd: number; risk_pct: number }>;
      pairs?: Array<{ left: string; right: string; correlation: number }>;
      [key: string]: unknown;
    };
    [key: string]: unknown;
  };
}

export interface TradingDeskSnapshot {
  schema_version: number;
  snapshot_id: string;
  created_at: string;
  environment: "research" | "paper" | "live";
  execution_mode: "research_only" | "paper_approval_required" | "live_disabled";
  decision: DeskDecision;
  headline: string;
  system: {
    platform_version: string;
    strategy_version: string;
    model_version: string;
    risk_policy_version: string;
    payoff_policy_version: string;
    commit_sha?: string | null;
  };
  market_phase: string;
  market_regime: string;
  market_regime_confidence?: number | null;
  funnel: {
    universe: number;
    chart_eligible: number;
    chart_candidates: number;
    deep_analyzed: number;
    positive_ev: number;
    risk_approved: number;
    trade_ready: number;
  };
  opportunities: TradingDeskOpportunity[];
  risk: TradingDeskRiskSummary;
  data_quality: {
    healthy: boolean;
    components: TradingDeskHealthComponent[];
    blocking_reasons: string[];
  };
  source_cycle?: number | null;
  warnings: string[];
}

export interface TradingDeskEvent {
  event_id: string;
  occurred_at: string;
  event_type: string;
  environment: string;
  payload: Record<string, unknown>;
}

export interface TradingDeskJournalEntry {
  journal_id: string;
  occurred_at: string;
  stage:
    | "evaluated"
    | "rejected"
    | "watch"
    | "trade_ready"
    | "proposed"
    | "approved"
    | "submitted"
    | "partial_fill"
    | "filled"
    | "exit_proposed"
    | "exited"
    | "cancelled"
    | "expired"
    | "error";
  environment: "research" | "paper" | "live";
  snapshot_id?: string | null;
  source_cycle?: number | null;
  symbol: string;
  contract_symbol?: string | null;
  decision?: DeskDecision | null;
  displayed?: boolean | null;
  composite_score?: number | null;
  ranking_score?: number | null;
  option_quality_score?: number | null;
  regime_fit_score?: number | null;
  expected_return_pct?: number | null;
  lower_confidence_bound_pct?: number | null;
  ev_samples?: number | null;
  hard_reasons: string[];
  watch_reasons: string[];
  broker_order_id?: string | null;
  fill_price?: number | null;
  realized_pnl_usd?: number | null;
}

async function json<T>(path: string): Promise<T> {
  const response = await fetch(path, { headers: authHeaders() });
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = await response.json();
      detail = body.detail || body.error || detail;
    } catch { /* ignore */ }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

export const tradingDeskApi = {
  getLatest: () => json<{
    status: string;
    snapshot: TradingDeskSnapshot | null;
    counts: { snapshots: number; events: number; journal?: number };
    store: string;
  }>("/api/trading-desk"),
  getEvents: (limit = 30) =>
    json<{ status: string; events: TradingDeskEvent[] }>(
      `/api/trading-desk/events?limit=${encodeURIComponent(String(limit))}`,
    ),
  getJournal: (limit = 50, symbol?: string) => {
    const query = new URLSearchParams({ limit: String(limit) });
    if (symbol) query.set("symbol", symbol);
    return json<{ status: string; journal: TradingDeskJournalEntry[] }>(
      `/api/trading-desk/journal?${query.toString()}`,
    );
  },
};
