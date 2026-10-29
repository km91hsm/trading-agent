// 장중 실시간 시세 (Phase 24 단계 2): 보이는 종목만 /api/quotes 로 짧은 주기 조회 → 표의 현재가·평가·NAV 갱신.
// 서버가 종목별 5초 캐시 + KIS 레이트리미터 안에서 부르므로 탭이 여럿이어도 KIS 호출은 늘지 않는다.
// 국내는 서버가 KIS 웹소켓 체결가를 받아 두므로(stream: true) 그동안은 1초마다 읽는다(KIS 호출 없음).
// 장 밖(session: closed)이면 1분마다 세션만 다시 확인한다. 가려진 탭은 쉰다.
import type { Position, PositionPlan } from "./types";

export const LIVE_INTERVAL_MS = 10_000;
export const STREAM_INTERVAL_MS = 1_000; // 서버 웹소켓 캐시를 읽을 뿐이라 싸다
export const CLOSED_RECHECK_MS = 60_000; // 장 밖: 1분마다 개장 여부만 확인(개장 순간을 빨리 잡는다)

export type LiveQuote = { price: string; change_pct: number | null; asof: string; stale: boolean };
export type QuotesResp = {
  session: "open" | "closed";
  asof: string;
  quotes: Record<string, LiveQuote>;
  stream?: boolean; // 서버 실시간 허브(웹소켓)가 붙어 있다
  degraded?: boolean; // 시세 요청이 실패했거나 TTL 이 지나 모든 시세를 stale 로 내렸다(2-W1)
  hub_yield?: "fills_listener" | "appkey_in_use" | null; // 실시간 허브가 붙지 않는 이유(2-P13)
};
export const QUOTE_TTL_MS = 30_000; // 이 안에 새 응답이 없으면 이전 시세를 '실시간' 으로 두지 않는다

/** 시세 요청 실패·TTL 초과: 모든 시세를 stale 로 내린다(표의 현재가·NAV·손절 거리가 스냅샷으로 돌아간다). */
export function degradeQuotes(resp: QuotesResp): QuotesResp {
  const quotes: Record<string, LiveQuote> = {};
  for (const [t, q] of Object.entries(resp.quotes)) quotes[t] = { ...q, stale: true };
  return { ...resp, quotes, degraded: true };
}

function kstHm(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const t = new Date(d.getTime() + 9 * 3_600_000);
  return `${String(t.getUTCHours()).padStart(2, "0")}:${String(t.getUTCMinutes()).padStart(2, "0")}`;
}
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
      const stop = p.stop && px > 0 ? { ...p.stop, dist_pct: dist(p.stop.level, px) } : p.stop;
      return { ...p, current_price: q.price, eval_amount: String(ev), live: true, change_pct: q.change_pct, plan, stop };
    }
    evalSum += before;
    return { ...p, live: false };
  });
  const base = snapshotNav === null || snapshotNav === undefined || snapshotNav === "" ? null : Number(snapshotNav);
  return { positions: out, nav: base === null ? null : base + (evalSum - snapEval), pnl: evalSum - cost, liveCount };
}

/** 다음 폴링까지 대기: 장중 웹소켓 1초 · 그 밖 장중 10초, 장 밖 1분. */
export function nextDelay(resp: QuotesResp | null): number {
  if (resp?.session !== "open") return CLOSED_RECHECK_MS;
  return resp.stream ? STREAM_INTERVAL_MS : LIVE_INTERVAL_MS;
}

/** 표 머리에 붙일 짧은 표기. */
export function liveLabel(resp: QuotesResp | null, liveCount: number): string {
  if (!resp) return "";
  if (resp.session === "closed") return "장 밖 · 마지막 잔고 기준";
  if (resp.degraded) {
    const at = kstHm(resp.asof);
    return at ? `시세 지연 · 마지막 ${at} 기준` : "시세 지연 · 마지막 응답 기준";
  }
  if (!liveCount) return "실시간 대기";
  if (resp.stream) return `실시간 ${liveCount}종목 · 체결 즉시`;
  const why = resp.hub_yield ? YIELD_LABEL[resp.hub_yield] : "";
  return `실시간 ${liveCount}종목 · 10초마다${why ? ` (${why})` : ""}`;
}

// 2-P13: 웹소켓이 붙지 않는 이유 — 실전 체결통보와 시세가 같은 앱키면 실전 전용 앱키를 발급한다
const YIELD_LABEL: Record<string, string> = {
  fills_listener: "실전 체결통보가 앱키 사용 중",
  appkey_in_use: "다른 연결이 앱키 사용 중",
};
