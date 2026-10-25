export type Market = "KR" | "US";

export type Account = {
  id: string;
  env: "paper" | "real";
  markets: Market[];
  mode: "auto" | "approve" | "off";
  yaml_mode: string;
  stance_by_market: Record<string, string>;
  nav_by_market: Record<string, string | null>;
  pending_approvals: number;
};

export type Position = {
  ticker: string;
  market: string;
  qty: number;
  avg_price: string | null;
  current_price: string | null;
  eval_amount: string | null;
  horizon?: "short" | "mid" | "long" | null; // 최근 결정의 보유기간 프로필 (Phase 23)
  horizon_days?: number | null;
};

export type Snapshot = { date: string; nav: string; cash: string; positions: Position[] } | null;

export type Order = {
  id: number;
  decision_id: number | null;
  account_id: string;
  ticker: string;
  market: string;
  side: string;
  qty: number;
  order_type: string;
  price: string | null;
  amount: string | null;
  status: string;
  order_no: string | null;
  message: string;
  mode: string;
  created_at: string;
};

export type Approval = {
  id: number;
  decision_id: number;
  account_id: string;
  status: string;
  requested_at: string;
  expires_at: string;
  decided_at: string | null;
  order: { ticker?: string; side?: string; qty?: number; price?: string | null; market?: string };
};

export type RiskCheck = {
  id: number;
  decision_id: number;
  account_id: string;
  approved: boolean;
  adjusted_qty: number | null;
  reasons: string[];
  created_at: string;
  ticker?: string;
  market?: string;
  date?: string;
  action?: string;
};

export type Decision = {
  id: number;
  date: string;
  market: string;
  ticker: string;
  action: string;
  target_weight: number | null;
  confidence: number | null;
  session_id: string | null;
  created_at: string;
};

export type Outcome = {
  account_id: string;
  approved: boolean;
  adjusted_qty: number | null;
  reasons: string[];
  checked_at: string;
  order_status: string | null;
  order_no: string | null;
  order_qty: number | null;
  order_price: string | null;
  order_mode: string | null;
};

export type Portfolio = {
  account_id: string;
  market: string;
  snapshot: Snapshot;
  sectors?: Array<{ sector: string; eval_amount: string; weight: number | null; tickers: string[]; count: number }>;
  max_sector_pct?: number;
  orders: Order[];
  approvals: Approval[];
};

export type Candle = {
  time: string | number; // 일봉 "YYYY-MM-DD", 분봉 UTC epoch 초
  open: string;
  high: string;
  low: string;
  close: string;
  volume: number;
};
export type Marker = { time: string | number; kind: "decision" | "order" | "trigger"; side: string; text: string };
export type ChartInterval = "1d" | "1m";
export type ChartData = {
  ticker: string;
  market: string;
  candles: Candle[];
  markers: Marker[];
  interval?: ChartInterval;
  tz?: string;
  session_date?: string | null;
};

export type Checkpoint = {
  stage: string;
  schema_version: number | null;
  data: unknown;
  call: unknown;
};

export type PerformanceBench = { return: number | null; series: Array<{ time: string; value: number }> };
export type PerformanceReport = {
  account_id: string;
  market: string;
  days: number;
  nav: Array<{ time: string; value: string }>;
  metrics: {
    return: number | null;
    mdd: number | null;
    sortino: number | null;
    days: number;
    start: string | null;
    end: string | null;
  };
  benchmarks: { one_over_n: PerformanceBench; buy_and_hold: PerformanceBench; quant: PerformanceBench | null };
  cost_sensitivity: Record<string, number>;
  turnover: number;
  decision_distribution: Record<string, number>;
  convergence_rate: number | null;
};

export type Run = {
  id: number;
  kind: string;
  market: string | null;
  started_at: string;
  finished_at: string | null;
  status: string;
  error: string | null;
};

export type CallsResp = {
  calls: Array<{ id: number; agent: string; model: string | null; cost_usd: number | null; created_at: string; status?: string }>;
  sessions_today: number;
  cost_today: number;
};

export type SystemSettings = {
  markets?: string[];
  risk?: Record<string, unknown>;
  debate_rounds?: number;
  web?: Record<string, unknown>;
  llm?: { budget_usd?: number; daily_session_cap?: number };
  models?: Record<string, string>;
};
