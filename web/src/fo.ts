// 파생 레인 화면(Phase 26)의 데이터 → 표 행 변환(순수 함수, vitest).
import { termLabel } from "./glossary";
import type { Account, FoDecisionRow, FoLeg, FoPosition } from "./types";

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

/** 계좌 모드 선택지 문구. 파생 계좌는 '선물옵션' 꼬리표. */
export function modeAccountLabel(a: Account): string {
  const env = a.env === "real" ? "실전" : "모의";
  return `${a.id} · ${env}${a.asset === "fo" ? " 선물옵션" : ""} · 현재 ${a.mode}`;
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

export function positionRows(ps: FoPosition[]): PositionRow[] {
  return ps.map((p) => {
    const kind = p.kind === "FUTURES" ? "" : `(${KIND[p.kind] ?? p.kind})`;
    const stops = [p.stop_pt ? `지수 ${pt(p.stop_pt)}` : null, p.premium_stop ? `프리미엄 ${pt(p.premium_stop)}` : null]
      .filter(Boolean)
      .join(" · ");
    return {
      id: p.id,
      code: p.code,
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
