// 장중 실시간 시세 (Phase 24 단계 2): 보이는 종목만 /api/quotes 로 짧은 주기 조회 → 표의 현재가·평가·NAV 갱신.
// 서버가 종목별 5초 캐시 + KIS 레이트리미터 안에서 부르므로 탭이 여럿이어도 KIS 호출은 늘지 않는다.
// 장 밖(session: closed)이면 1분마다 세션만 다시 확인한다. 가려진 탭은 쉰다.
import type { Position, PositionPlan } from "./types";

export const LIVE_INTERVAL_MS = 10_000;
export const CLOSED_RECHECK_MS = 60_000; // 장 밖: 1분마다 개장 여부만 확인(개장 순간을 빨리 잡는다)

export type LiveQuote = { price: string; change_pct: number | null; asof: string; stale: boolean };
export type QuotesResp = { session: "open" | "closed"; asof: string; quotes: Record<string, LiveQuote> };
export type LivePosition = Position & { live?: boolean; change_pct?: number | null };

function dist(target: number | null | undefined, price: number): number | null {
  return target === null || target === undefined || !(target > 0) ? null : target / price - 1;
}

/** 결정 계획의 익절·손절·지정가 거리를 실시간 현재가로 다시 잰다(서버 `views._dist` 와 같은 식).
 * 스냅샷 거리를 그대로 두면 시세가 움직여도 '손절까지 −3%' 가 멈춰 있다(전체 검토 W8). */
export function livePlan(plan: PositionPlan | null | undefined, price: number): PositionPlan | null {
  if (!plan) return null;
  if (!(price > 0)) return plan;
  return {
    ...plan,
    dist_take_profit_pct: dist(plan.take_profit, price),
    dist_stop_pct: dist(plan.stop_loss, price),
    dist_limit_pct: dist(plan.limit_price, price),
  };
}

/** 보유 행에 실시간 현재가를 입힌다. 평가 = 수량 × 현재가, 계획 거리도 다시 잰다(livePlan).
 * NAV 는 스냅샷의 브로커 NAV(KR 총평가 — 미결제 매수대금이 현금에 남아 있어 '현금 + 평가' 로 다시 만들면
 * 이중으로 센다)에 **평가 변화분만** 더한다. stale 값은 쓰지 않는다(스냅샷이 이미 같은 값). */
export function applyQuotes(
  positions: Position[],
  quotes: Record<string, LiveQuote> | null | undefined,
  snapshotNav?: string | null,
): { positions: LivePosition[]; nav: number | null; pnl: number; liveCount: number } {
  let evalSum = 0;
  let snapEval = 0;
  let cost = 0;
  let liveCount = 0;
  const out = positions.map((p): LivePosition => {
    const q = quotes?.[p.ticker];
    const avg = Number(p.avg_price ?? 0);
    cost += avg * p.qty;
    const before = Number(p.eval_amount ?? 0);
    snapEval += before;
    if (q && !q.stale && Number.isFinite(Number(q.price))) {
      const px = Number(q.price);
      const ev = px * p.qty;
      evalSum += ev;
      liveCount += 1;
      const plan = p.plan ? livePlan(p.plan, px) : p.plan;
      return { ...p, current_price: q.price, eval_amount: String(ev), live: true, change_pct: q.change_pct, plan };
    }
    evalSum += before;
    return { ...p, live: false };
  });
  const base = snapshotNav === null || snapshotNav === undefined || snapshotNav === "" ? null : Number(snapshotNav);
  return { positions: out, nav: base === null ? null : base + (evalSum - snapEval), pnl: evalSum - cost, liveCount };
}

/** 다음 폴링까지 대기: 장중 10초, 장 밖 1분. */
export function nextDelay(resp: QuotesResp | null): number {
  return resp?.session === "open" ? LIVE_INTERVAL_MS : CLOSED_RECHECK_MS;
}

/** 표 머리에 붙일 짧은 표기. */
export function liveLabel(resp: QuotesResp | null, liveCount: number): string {
  if (!resp) return "";
  if (resp.session === "closed") return "장 밖 · 마지막 잔고 기준";
  return liveCount ? `실시간 ${liveCount}종목 · 10초마다` : "실시간 대기";
}
