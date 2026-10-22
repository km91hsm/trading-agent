// 숫자·시각 표시 헬퍼. 금액은 API 의 Decimal 문자열을 그대로 받고, 시각은 UTC ISO 를 시장 현지로 바꾼다.
import type { Market } from "./types";

const DASH = "—";

function num(v: string | number | null | undefined): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

export function fmtMoney(v: string | number | null | undefined, market: Market | string): string {
  const n = num(v);
  if (n === null) return DASH;
  if (market === "US") {
    const sign = n < 0 ? "-" : "";
    return `${sign}$${Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  }
  return `${Math.round(n).toLocaleString("en-US")}원`;
}

/** 비율(0.0153) → "1.53%". 이미 퍼센트 단위면 호출 전에 /100 한다. */
export function fmtPct(v: number | string | null | undefined, digits = 2): string {
  const n = num(v);
  if (n === null) return DASH;
  return `${(n * 100).toFixed(digits)}%`;
}

export function round2(n: number): number {
  return Math.round(n * 100) / 100;
}

export function fmtDelta(
  cur: string | number | null | undefined,
  prev: string | number | null | undefined,
): { abs: number; pct: number } | null {
  const c = num(cur);
  const p = num(prev);
  if (c === null || p === null || p === 0) return null;
  return { abs: round2(c - p), pct: round2(((c - p) / p) * 100) };
}

const TZ: Record<string, string> = { KR: "Asia/Seoul", US: "America/New_York" };

export function fmtTime(iso: string | null | undefined, market: Market | string): string {
  if (!iso) return DASH;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return DASH;
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: TZ[market] ?? "Asia/Seoul",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(d);
}

/** ISO 시각 → KST 날짜 "YYYY-MM-DD". 이미 날짜 문자열이면 그대로. */
export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return DASH;
  if (/^\d{4}-\d{2}-\d{2}$/.test(iso)) return iso;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return DASH;
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Seoul",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(d);
}

/** 만료까지 남은 분(음수면 0). */
export function minutesLeft(expiresIso: string | null | undefined, now = new Date()): number | null {
  if (!expiresIso) return null;
  const t = new Date(expiresIso).getTime();
  if (Number.isNaN(t)) return null;
  return Math.max(0, Math.round((t - now.getTime()) / 60000));
}
