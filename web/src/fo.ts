// 파생 레인 화면(Phase 26)의 데이터 → 표 행 변환(순수 함수, vitest).
import { termLabel } from "./glossary";
import type { Account, FoCheck, FoContext, FoDecisionRow, FoLeg, FoPosition, FoState, FoStatus } from "./types";

const DIRECTION: Record<string, string> = { LONG: "롱", SHORT: "숏", FLAT: "관망" };
const VEHICLE: Record<string, string> = { FUTURES: "선물", OPTION_BUY: "옵션 매수", OPTION_SELL: "옵션 매도" };
const KIND: Record<string, string> = { FUTURES: "선물", CALL: "콜", PUT: "풋" };
const SIDE: Record<string, string> = { LONG: "매수", SHORT: "매도" };
const INTENT: Record<string, string> = {
  OPEN: "진입", RESIZE: "배수 조정", HOLD: "유지", CLOSE: "청산", REPLACE: "교체",
  RESIZE_UP: "증액", RESIZE_DOWN: "감액", STOP: "손절", EXPIRY: "만기 청산",
  SESSION_CLOSE: "마감 청산", RISK_REDUCE: "위험 축소",
};
/** 사람이 봐야 하는 상태(황색). */
export const ATTENTION = new Set(["unknown", "exit_failed", "awaiting_settlement"]);

/** 계좌 목록을 현물·파생으로 나눈다. asset 이 없으면(옛 서버) 현물. */
export function splitAccounts(rows: Account[]): { stock: Account[]; fo: Account[] } {
  return { stock: rows.filter((a) => a.asset !== "fo"), fo: rows.filter((a) => a.asset === "fo") };
}

/** 설정 화면(계좌 모드)의 계좌 목록 — 계좌를 가리지 않는 제어라 현물 뒤에 파생 계좌까지. */
export function settingsAccounts(stock: Account[], fo: Account[]): Account[] {
  return [...stock, ...fo];
}

const MODE_KO: Record<string, string> = { auto: "자동", approve: "승인", off: "끔" };

/** 계좌 선택지 문구 "real-2 · 실전 · 승인"(상단 선택기·설정·파생 화면 공통). 파생 계좌는 '선물옵션' 꼬리표. */
export function modeAccountLabel(a: Account): string {
  const env = a.env === "real" ? "실전" : "모의";
  return `${a.id} · ${env}${a.asset === "fo" ? " 선물옵션" : ""} · ${MODE_KO[a.mode] ?? a.mode}`;
}

export const directionLabel = (d: string | null | undefined) => (d ? DIRECTION[d] ?? d : "—");
export const vehicleLabel = (v: string | null | undefined) => (v ? VEHICLE[v] ?? v : "—");
export const intentLabel = (i: string | null | undefined) => (i ? INTENT[i] ?? i : "—");
export const statusLabel = (s: string) => termLabel(`fo.status.${s}`);

function num(v: string | number | null | undefined): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

function pt(v: string | number | null | undefined): string {
  const n = num(v);
  return n === null ? "—" : n.toFixed(2);
}

export type PositionRow = {
  id: number;
  code: string;
  avg: string; // 평균 체결가(pt)
  current: string;
  pnl: string; // 평가손익(브로커 값, 원)
  pnlDir: "up" | "dn" | "flat";
  tp: string; // 익절선(지수)
  stopFut: string; // 선물 가격으로 환산한 손절가
  daysLeft: string; // 최종거래일까지 "D-15"
  horizon: string; // 보유 기한
  opened: string; // 진입일
  what: string; // "롱 · 선물" / "숏 · 옵션 매수(풋)"
  side: string; // 계약 보유 방향 "매수 2계약"
  dir: "up" | "dn" | "flat"; // 적청 데이터 색(base.css .up·.dn)
  leverage: string;
  stop: string; // 손절선(지수)·보호선
  budget: string; // "12만 + 계획 50만 / 200만 · 여유 138만"
  status: string;
  attention: boolean;
};

function won(v: string | null): string {
  const n = num(v);
  if (n === null) return "—";
  const abs = Math.abs(n);
  if (abs >= 1e8) return `${(n / 1e8).toFixed(2)}억`;
  if (abs >= 1e4) return `${Math.round(n / 1e4).toLocaleString("ko-KR")}만`;
  return Math.round(n).toLocaleString("ko-KR");
}

/** 최종거래일까지 남은 날 "D-15"·"D-day"·"만기 지남". 모르면 "—". */
export function daysLeft(last: string | null, today: string): string {
  if (!last) return "—";
  const d = Math.round((Date.parse(`${last}T00:00:00Z`) - Date.parse(`${today}T00:00:00Z`)) / 86_400_000);
  if (!Number.isFinite(d)) return "—";
  return d > 0 ? `D-${d}` : d === 0 ? "D-day" : "만기 지남";
}

const kstDate = (iso: string | null) => (iso ? new Date(iso).toLocaleDateString("sv-SE", { timeZone: "Asia/Seoul" }) : "—");
const kstTime = (iso: string | null) =>
  iso ? new Date(iso).toLocaleTimeString("ko-KR", { timeZone: "Asia/Seoul", hour: "2-digit", minute: "2-digit", hour12: false }) : "—";

export function positionRows(ps: FoPosition[], today: string = kstDate(new Date().toISOString())): PositionRow[] {
  return ps.map((p) => {
    const pnl = num(p.eval);
    const kind = p.kind === "FUTURES" ? "" : `(${KIND[p.kind] ?? p.kind})`;
    const stops = [p.stop_pt ? `지수 ${pt(p.stop_pt)}` : null, p.premium_stop ? `프리미엄 ${pt(p.premium_stop)}` : null]
      .filter(Boolean)
      .join(" · ");
    return {
      id: p.id,
      code: p.code,
      avg: pt(p.avg),
      current: pt(p.current),
      pnl: won(p.eval),
      pnlDir: pnl === null || pnl === 0 ? "flat" : pnl > 0 ? "up" : "dn",
      tp: p.tp_pt ? `지수 ${pt(p.tp_pt)}` : "—",
      stopFut: pt(p.stop_fut),
      daysLeft: daysLeft(p.last_trade_date, today),
      horizon: p.horizon_until ? p.horizon_until.slice(0, 10) : "—",
      opened: kstDate(p.opened_at),
      what: `${directionLabel(p.direction)} · ${vehicleLabel(p.vehicle)}${kind}`,
      side: `${SIDE[p.contract_side] ?? p.contract_side} ${p.qty_held}계약` + (p.qty_target !== p.qty_held ? ` → ${p.qty_target}` : ""),
      dir: p.direction === "LONG" ? "up" : p.direction === "SHORT" ? "dn" : "flat",
      leverage: p.leverage_used === null ? "—" : `${p.leverage_used.toFixed(2)}배`,
      stop: stops || "—",
      budget:
        `${won(p.budget_used)}` +
        (p.budget_planned ? ` + 계획 ${won(p.budget_planned)}` : "") +
        ` / ${won(p.budget_limit)}` +
        (p.budget_free ? ` · 여유 ${won(p.budget_free)}` : p.budget_planned === undefined ? "" : " · 여유 미확인"),
      status: statusLabel(p.status),
      attention: ATTENTION.has(p.status),
    };
  });
}

export type LegRow = {
  id: number;
  code: string;
  intent: string;
  progress: string; // "1/2계약"
  price: string;
  status: string; // 주문 상태 라벨(공용 status.*)
  attempt: string;
  live: boolean;
  orderNo: string;
  sentAt: string; // KST HH:MM
};

const LIVE = new Set(["prepared", "sending", "accepted", "unknown"]);

export function legRows(ls: FoLeg[]): LegRow[] {
  return ls.map((l) => ({
    id: l.id,
    code: l.code,
    intent: intentLabel(l.intent) + (l.round > 0 ? ` · 재평가 ${l.round}` : ""),
    progress: `${l.filled_qty}/${l.qty}계약`,
    price: l.order_type === "MARKET" ? "시장가" : l.price ? `${pt(l.price)}pt` : "—",
    status: l.status === "prepared" ? "준비" : l.status === "sending" ? "전송 중" : termLabel(`status.${l.status}`),
    attempt: l.attempt > 1 ? `${l.attempt}차` : "",
    live: LIVE.has(l.status),
    orderNo: l.order_no ?? "—",
    sentAt: kstTime(l.sent_at),
  }));
}

/** 결정 한 줄: "진입 롱 · 선물 · 배수 1.00 · 확신 0.70". */
export function decisionLine(d: FoDecisionRow | null): string {
  if (!d) return "오늘 결정 없음";
  const parts = [`${intentLabel(d.intent)} ${directionLabel(d.direction)}`, vehicleLabel(d.vehicle)];
  if (d.target_leverage !== null) parts.push(`배수 ${d.target_leverage.toFixed(2)}`);
  if (d.confidence !== null) parts.push(`확신 ${d.confidence.toFixed(2)}`);
  if (d.record_only) parts.push(`기록만(${d.record_only})`);
  return parts.join(" · ");
}

const STATE: Record<FoState, { label: string; kind: "ok" | "neutral" | "pending" | "danger" }> = {
  active: { label: "자동", kind: "ok" },
  record_only: { label: "기록만", kind: "neutral" },
  halted: { label: "신규 금지(손실 한도)", kind: "danger" },
  unverified: { label: "검증 전", kind: "pending" },
  disabled: { label: "레인 꺼짐", kind: "neutral" },
  off: { label: "실전 미지원", kind: "neutral" },
};

/** 파생 계좌 상태 배지(라벨·색). */
export const stateBadge = (s: FoState) => STATE[s] ?? { label: s, kind: "neutral" as const };

/** 상태 배지에 붙이는 설명 — 왜 거래가 없는지. */
export const STATE_HINT: Record<FoState, string> = {
  active: "자동 매매 중(진입 창·게이트 통과 시 주문)",
  record_only: "모드가 자동이 아니거나 일시정지라 판정만 기록",
  halted: "오늘 손실 한도에 닿아 신규 진입을 막음(보유 축소·청산은 계속)",
  unverified: "수단별 실기 검증(order-test) 전이라 신규 진입이 닫혀 있음",
  disabled: "설정 fo.enabled 가 꺼져 있어 신규 진입 없음(보유 감시·축소는 계속)",
  off: "실전 파생 계좌는 이번 범위 밖이라 주문하지 않음",
};

export type SummaryPart = { text: string; tone?: "danger" | "up" | "dn" };

/** 대시보드 한 줄 요약 조각: 순자산 · 증거금 · 포지션 · 오늘 결정. */
export function summaryParts(s: FoStatus): SummaryPart[] {
  const nav = num(s.nav);
  const add = num(s.add_margin);
  const margin: SummaryPart =
    s.margin_pct === null
      ? { text: "증거금 미확인" }
      : add && add > 0
        ? { text: `증거금 ${Math.round(s.margin_pct * 100)}% · 추가증거금 ${won(s.add_margin)}`, tone: "danger" }
        : { text: `증거금 ${Math.round(s.margin_pct * 100)}%` };
  const ps = s.positions;
  const first = ps[0];
  const position: SummaryPart = first
    ? {
        text: `${first.code} ${SIDE[first.contract_side] ?? first.contract_side} ${first.qty}계약` + (ps.length > 1 ? ` 외 ${ps.length - 1}건` : ""),
        tone: first.direction === "LONG" ? "up" : first.direction === "SHORT" ? "dn" : undefined,
      }
    : { text: "포지션 없음" };
  return [
    { text: nav === null ? "순자산 미확인" : `순자산 ${won(s.nav)}` },
    margin,
    position,
    { text: s.decision ? `오늘: ${decisionLine(s.decision)}` : "오늘 결정 없음" },
  ];
}

/** 허용 수단의 실기 검증 여부 "선물 ✓ · 옵션 매수 – · 옵션 매도 –". */
export function verifiedText(s: FoStatus): string {
  return s.vehicles.map((v) => `${vehicleLabel(v)} ${s.verified[v] ? "✓" : "–"}`).join(" · ");
}

/** 게이트 판정 상세(배수·노출·스트레스 손실·수명 예산·대조 배수·목표 계약). 상세가 없으면 빈 목록. */
export function checkDetail(c: FoCheck): string[] {
  const out: string[] = [];
  if (c.leverage_used !== undefined && c.leverage_used !== null) out.push(`배수 ${c.leverage_used.toFixed(2)}`);
  if (c.exposure_after) out.push(`노출 ${won(c.exposure_after)}`);
  if (c.stress_loss_after) out.push(`스트레스 손실 ${won(c.stress_loss_after)}`);
  const b = c.lifetime_budget;
  if (b) out.push(`수명 예산 ${won(b.used)} + 계획 ${won(b.planned)} / ${won(b.limit)}`);
  if (c.cross_factor !== undefined && c.cross_factor !== null) out.push(`대조 배수 ${c.cross_factor}`);
  if (c.n_target !== undefined && c.n_target !== null) out.push(`목표 ${c.n_target}계약`);
  return out;
}

const REGIME: Record<string, string> = { bull: "상승", neutral: "중립", bear: "하락" };
const STANCE: Record<string, string> = { RISK_ON: "위험 선호", RISK_OFF: "위험 회피", NEUTRAL: "중립" };

/** 시장·한도 표 행 [이름, 값]. */
export function contextRows(c: FoContext): [string, string][] {
  const lim = (c.limits ?? {}) as Record<string, unknown>;
  const n = (k: string) => (typeof lim[k] === "number" ? (lim[k] as number) : null);
  const idx = c.index?.price !== undefined && c.index?.price !== null ? `${pt(c.index.price)}${c.index.asof ? ` (${kstTime(c.index.asof)})` : ""}` : "—";
  const reg = c.regime?.state ? `${REGIME[c.regime.state] ?? c.regime.state}${typeof c.regime.count === "number" ? ` (${c.regime.count}/3)` : ""}` : "—";
  const mv = c.market_view?.stance
    ? `${STANCE[c.market_view.stance] ?? c.market_view.stance}${typeof c.market_view.confidence === "number" ? ` · 확신 ${c.market_view.confidence.toFixed(2)}` : ""}`
    : "제안 없음";
  const maxLev = n("max_leverage");
  const cap = lim.regime_cap;
  const capText = cap === 0 ? " · 국면 상한 신규 금지" : typeof cap === "number" ? ` · 국면 상한 ${cap.toFixed(2)}배` : "";
  const win = lim.entry_window as { start?: string; cutoff?: string } | undefined;
  return [
    ["코스피200", idx],
    ["국면", reg],
    ["전략가 제안", mv],
    ["최대 배수", maxLev === null ? "—" : `${maxLev.toFixed(2)}배${capText}`],
    ["최대 계약", n("max_contracts") === null ? "—" : `${n("max_contracts")}계약`],
    ["1회 위험", n("risk_per_trade_pct") === null ? "—" : `${((n("risk_per_trade_pct") as number) * 100).toFixed(1)}%`],
    ["진입 창", win?.start && win?.cutoff ? `${win.start}~${win.cutoff}` : "—"],
  ];
}

/** 결정 상세: 손절선·익절선·보유 기간·결정 때 지수·현물과의 관계. */
export function decisionDetail(d: FoDecisionRow | null): string[] {
  if (!d) return [];
  const out: string[] = [];
  if (d.stop_pt !== null && d.stop_pt !== undefined) out.push(`손절선 지수 ${pt(d.stop_pt)}`);
  if (d.take_profit_pt !== null && d.take_profit_pt !== undefined) out.push(`익절선 지수 ${pt(d.take_profit_pt)}`);
  if (d.horizon_days) out.push(`보유 ${d.horizon_days}일`);
  if (d.index_at_decision) out.push(`결정 때 지수 ${pt(d.index_at_decision)}`);
  if (d.vs_spot) out.push(`현물 대비: ${d.vs_spot}`);
  return out;
}

