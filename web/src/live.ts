// 장중 실시간 시세 (Phase 24 단계 2): 보이는 종목만 /api/quotes 로 짧은 주기 조회 → 표의 현재가·평가·NAV 갱신.
// 서버가 종목별 5초 캐시 + KIS 레이트리미터 안에서 부르므로 탭이 여럿이어도 KIS 호출은 늘지 않는다.
// 장 밖(session: closed)이면 5분마다 세션만 다시 확인한다. 가려진 탭은 쉰다.
import type { Position } from "./types";

export const LIVE_INTERVAL_MS = 10_000;
export const CLOSED_RECHECK_MS = 300_000;

export type LiveQuote = { price: string; change_pct: number | null; asof: string; stale: boolean };
export type QuotesResp = { session: "open" | "closed"; asof: string; quotes: Record<string, LiveQuote> };
export type LivePosition = Position & { live?: boolean; change_pct?: number | null };

/** 보유 행에 실시간 현재가를 입힌다. 평가 = 수량 × 현재가, NAV = 현금 + 평가 합(현금은 스냅샷 그대로).
 * stale 값은 쓰지 않는다(스냅샷이 이미 같은 값을 갖고 있다). */
export function applyQuotes(
  positions: Position[],
  quotes: Record<string, LiveQuote> | null | undefined,
  cash?: string | null,
): { positions: LivePosition[]; nav: number | null; pnl: number; liveCount: number } {
  let evalSum = 0;
  let cost = 0;
  let liveCount = 0;
  const out = positions.map((p): LivePosition => {
    const q = quotes?.[p.ticker];
    const avg = Number(p.avg_price ?? 0);
    cost += avg * p.qty;
    if (q && !q.stale && Number.isFinite(Number(q.price))) {
      const px = Number(q.price);
      const ev = px * p.qty;
      evalSum += ev;
      liveCount += 1;
      return { ...p, current_price: q.price, eval_amount: String(ev), live: true, change_pct: q.change_pct };
    }
    evalSum += Number(p.eval_amount ?? 0);
    return { ...p, live: false };
  });
  const c = cash === null || cash === undefined || cash === "" ? null : Number(cash);
  return { positions: out, nav: c === null ? null : c + evalSum, pnl: evalSum - cost, liveCount };
}

/** 다음 폴링까지 대기: 장중 10초, 장 밖 5분. */
export function nextDelay(resp: QuotesResp | null): number {
  return resp?.session === "open" ? LIVE_INTERVAL_MS : CLOSED_RECHECK_MS;
}

/** 표 머리에 붙일 짧은 표기. */
export function liveLabel(resp: QuotesResp | null, liveCount: number): string {
  if (!resp) return "";
  if (resp.session === "closed") return "장 마감 · 스냅샷 값";
  return liveCount ? `실시간 ${liveCount}종목 · 10초마다` : "실시간 대기";
}
