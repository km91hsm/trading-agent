export type Market = "KR" | "US";

export type Account = {
  id: string;
  env: "paper" | "real";
  markets: Market[];
  mode: "auto" | "approve" | "off";
  yaml_mode: string;
  stance: string;
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
  orders: Order[];
  approvals: Approval[];
};

export type Candle = {
  time: string;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: number;
};
export type Marker = { time: string; kind: "decision" | "order"; side: string; text: string };
export type ChartData = { ticker: string; market: string; candles: Candle[]; markers: Marker[] };

export type Checkpoint = {
  stage: string;
  schema_version: number | null;
  data: unknown;
  call: unknown;
};
