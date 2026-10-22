// 대시보드 순수 함수: 장 상태 라벨, 오늘의 시세표 행 조립. 화면과 분리해 vitest 로 고정한다.
import { fmtMoney, minutesLeft } from "./format";
import { termLabel } from "./glossary";
import type { Approval, Decision, Order, Position, RiskCheck } from "./types";

export type Phase = "pre" | "open" | "closed" | "weekend";

const PHASE_LABEL: Record<Phase, string> = { pre: "개장 전", open: "정규장", closed: "마감", weekend: "휴장" };

function localParts(d: Date, tz: string): { weekday: number; minutes: number } {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: tz,
    weekday: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).formatToParts(d);
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? "";
  const wd = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].indexOf(get("weekday"));
  const h = Number(get("hour")) % 24;
  const m = Number(get("minute"));
  return { weekday: wd, minutes: h * 60 + m };
}

function phase(d: Date, tz: string, open: number, close: number): Phase {
  const { weekday, minutes } = localParts(d, tz);
  if (weekday === 0 || weekday === 6) return "weekend";
  if (minutes < open) return "pre";
  if (minutes < close) return "open";
  return "closed";
}

/** KR 09:00~15:30 KST, US 09:30~16:00 ET (휴장일은 모른다 — 주말만 본다). */
export function marketPhase(now: Date): { kr: Phase; us: Phase; label: string } {
  const kr = phase(now, "Asia/Seoul", 9 * 60, 15 * 60 + 30);
  const us = phase(now, "America/New_York", 9 * 60 + 30, 16 * 60);
  return { kr, us, label: `KR ${PHASE_LABEL[kr]} · US ${PHASE_LABEL[us]}` };
}


export type TapeRow = {
  key: string;
  ticker: string;
  market: string;
  mark: "held" | "pick" | "watch" | null;
  qty: number;
  price: string | null;
  evalAmount: string | null;
  weight: number | null;
  action: string | null;
  confidence: number | null;
  decisionId: number | null;
  gate: { approved: boolean | null; text: string };
};

export type TapeInput = {
  positions: Position[];
  decisions: Decision[];
  riskChecks: RiskCheck[];
  orders: Order[];
  approvals: Approval[];
  watch: string[];
  dates: Record<string, string>; // 시장별 오늘(운영일)
  nav?: Record<string, string | null | undefined>;
  now?: Date;
};

const MARK_ORDER = { held: 0, pick: 1, watch: 2 } as const;

/** "KR:005930"·"US:AAPL"·"005930"(6자리 숫자 = KR) → [market, ticker] */
export function parseWatch(w: string): [string, string] {
  const m = w.match(/^(KR|US):(.+)$/i);
  if (m) return [m[1].toUpperCase(), m[2]];
  return [/^\d{6}$/.test(w) ? "KR" : "US", w];
}

export function gateFor(
  d: Decision,
  rc: RiskCheck | undefined,
  order: Order | undefined,
  approval: Approval | undefined,
  now: Date,
): { approved: boolean | null; text: string } {
  if (!rc) return { approved: null, text: "판정 없음" };
  if (d.action === "HOLD") return { approved: null, text: "보유 · 주문 없음" };
  if (!rc.approved) return { approved: false, text: `거부 · ${rc.reasons[0] ?? "사유 없음"}` };
  const qty = rc.adjusted_qty ?? 0;
  if (order) {
    const amt = order.amount ? ` · ${fmtMoney(order.amount, d.market)}` : "";
    return { approved: true, text: `${order.qty}주 ${termLabel(`status.${order.status}`)}${amt}` };
  }
  if (approval && approval.status === "pending") {
    const left = minutesLeft(approval.expires_at, now);
    return { approved: true, text: `${qty}주 승인 대기${left === null ? "" : ` · ${left}분 뒤 만료`}` };
  }
  if (qty > 0) return { approved: true, text: `${qty}주 승인 · 주문 없음` };
  return { approved: true, text: "통과 · 주문 없음" };
}

/** 보유·오늘 결정·관심종목을 한 줄씩 합친 '오늘의 시세표' 행. */
export function buildTape(input: TapeInput): TapeRow[] {
  const now = input.now ?? new Date();
  const rows = new Map<string, TapeRow>();
  const key = (market: string, ticker: string) => `${market}:${ticker}`;
  const navOf = (market: string) => {
    const v = input.nav?.[market];
    const n = v ? Number(v) : NaN;
    return Number.isFinite(n) && n > 0 ? n : null;
  };
  for (const p of input.positions) {
    if (!p.qty) continue;
    const nav = navOf(p.market);
    const ev = p.eval_amount ? Number(p.eval_amount) : NaN;
    rows.set(key(p.market, p.ticker), {
      key: key(p.market, p.ticker),
      ticker: p.ticker,
      market: p.market,
      mark: "held",
      qty: p.qty,
      price: p.current_price,
      evalAmount: p.eval_amount,
      weight: nav && Number.isFinite(ev) ? ev / nav : null,
      action: null,
      confidence: null,
      decisionId: null,
      gate: { approved: null, text: "분석 없음" },
    });
  }
  const todays = input.decisions.filter((d) => input.dates[d.market] === d.date);
  const latestByKey = new Map<string, Decision>();
  for (const d of todays) {
    const k = key(d.market, d.ticker);
    const cur = latestByKey.get(k);
    if (!cur || d.id > cur.id) latestByKey.set(k, d);
  }
  for (const d of latestByKey.values()) {
    const k = key(d.market, d.ticker);
    const rc = input.riskChecks.filter((r) => r.decision_id === d.id).sort((a, b) => b.id - a.id)[0];
    const order = input.orders.filter((o) => o.decision_id === d.id).sort((a, b) => b.id - a.id)[0];
    const approval = input.approvals.filter((a) => a.decision_id === d.id).sort((a, b) => b.id - a.id)[0];
    const base = rows.get(k);
    const row: TapeRow = base ?? {
      key: k,
      ticker: d.ticker,
      market: d.market,
      mark: "pick",
      qty: 0,
      price: null,
      evalAmount: null,
      weight: null,
      action: null,
      confidence: null,
      decisionId: null,
      gate: { approved: null, text: "" },
    };
    row.action = d.action;
    row.confidence = d.confidence;
    row.decisionId = d.id;
    row.gate = gateFor(d, rc, order, approval, now);
    rows.set(k, row);
  }
  for (const w of input.watch) {
    const [market, ticker] = parseWatch(w);
    const k = key(market, ticker);
    if (rows.has(k)) continue;
    rows.set(k, {
      key: k,
      ticker,
      market,
      mark: "watch",
      qty: 0,
      price: null,
      evalAmount: null,
      weight: null,
      action: null,
      confidence: null,
      decisionId: null,
      gate: { approved: null, text: "분석 없음" },
    });
  }
  return [...rows.values()].sort((a, b) => {
    const ma = MARK_ORDER[a.mark ?? "watch"];
    const mb = MARK_ORDER[b.mark ?? "watch"];
    if (ma !== mb) return ma - mb;
    if (a.market !== b.market) return a.market < b.market ? -1 : 1;
    return a.ticker < b.ticker ? -1 : a.ticker > b.ticker ? 1 : 0;
  });
}
