// 전체 계좌 합산(/api/portfolio/overview)과 보유 행 계획 열 — 행 변환 순수 함수 (Phase 24).
import { fmtMoney } from "./format";
import type { Market, Overview, OverviewEnv, Position, PositionPlan } from "./types";

export const ALL_ACCOUNTS = "all";

/** "110,000원 (+10.0%)" — 가격이 없으면 "—", 거리가 없으면 가격만. */
export function planCell(price: number | null | undefined, dist: number | null | undefined, market: Market | string): string {
  if (price === null || price === undefined) return "—";
  const p = fmtMoney(price, market);
  if (dist === null || dist === undefined) return p;
  const sign = dist > 0 ? "+" : "";
  return `${p} (${sign}${(dist * 100).toFixed(1)}%)`;
}

/** 거리 부호 → 표 색 클래스: 위(익절 쪽)는 up, 아래(손절 쪽)는 dn. */
export function distClass(dist: number | null | undefined): string {
  if (dist === null || dist === undefined || dist === 0) return "";
  return dist > 0 ? "up" : "dn";
}

export type PlanCells = { target: string; takeProfit: string; stop: string; limit: string; tpClass: string; stopClass: string };

export function planCells(plan: PositionPlan | null | undefined, market: Market | string): PlanCells | null {
  if (!plan) return null;
  return {
    target: plan.target_weight === null || plan.target_weight === undefined ? "—" : `${(plan.target_weight * 100).toFixed(0)}%`,
    takeProfit: planCell(plan.take_profit, plan.dist_take_profit_pct, market),
    stop: planCell(plan.stop_loss, plan.dist_stop_pct, market),
    limit: planCell(plan.limit_price, plan.dist_limit_pct, market),
    tpClass: distClass(plan.dist_take_profit_pct),
    stopClass: distClass(plan.dist_stop_pct),
  };
}

export type OverviewRow = {
  market: Market;
  env: "paper" | "real";
  nav: string;
  cash: string;
  pnl: string;
  pnlPct: number | null;
  holdings: number;
  accounts: number;
  data: OverviewEnv;
};

/** 시장 × 모의/실전 행. 통화가 달라 시장끼리, 가짜 돈과 진짜 돈이라 모의·실전끼리 더하지 않는다
 * (PRODUCT.md 원칙 3) — 보유·섹터도 행마다 따로다. 모의가 먼저. */
export function overviewRows(o: Overview | null | undefined): OverviewRow[] {
  if (!o) return [];
  const out: OverviewRow[] = [];
  for (const m of ["KR", "US"] as Market[]) {
    for (const env of ["paper", "real"] as const) {
      const e = o.markets[m]?.envs?.[env];
      if (!e) continue;
      out.push({
        market: m,
        env,
        nav: fmtMoney(e.nav, m),
        cash: fmtMoney(e.cash, m),
        pnl: fmtMoney(e.pnl, m),
        pnlPct: e.pnl_pct,
        holdings: e.positions.length,
        accounts: e.accounts.length,
        data: e,
      });
    }
  }
  return out;
}

/** 스냅샷이 아직 없는 계좌 "KR: a, b · US: c". 없으면 "". */
export function missingText(o: Overview | null | undefined): string {
  if (!o) return "";
  return (["KR", "US"] as Market[])
    .filter((m) => (o.accounts_missing_snapshot[m] ?? []).length)
    .map((m) => `${m}: ${(o.accounts_missing_snapshot[m] ?? []).join(", ")}`)
    .join(" · ");
}


/** '전체' 시세표용 보유: 같은 시장·종목을 모든 계좌·환경에서 합친다(수량·평가 합, 계좌 목록 합집합).
 * 한 계좌의 수량만 남기던 버그(전체 검토 W4) 대신 합계를 보이고 출처는 accounts 로 남긴다. */
export function mergeHoldings(rows: Position[]): Position[] {
  const by = new Map<string, Position>();
  for (const p of rows) {
    const k = `${p.market}:${p.ticker}`;
    const cur = by.get(k);
    if (!cur) {
      by.set(k, { ...p, accounts: [...(p.accounts ?? [])] });
      continue;
    }
    const ev = Number(cur.eval_amount ?? 0) + Number(p.eval_amount ?? 0);
    by.set(k, {
      ...cur,
      qty: cur.qty + p.qty,
      eval_amount: String(ev),
      accounts: Array.from(new Set([...(cur.accounts ?? []), ...(p.accounts ?? [])])),
    });
  }
  return [...by.values()];
}

export type AccountRows<T> = { rows: T[]; failed: string[] };

/** 계좌마다 부른 조회를 합친다. 실패한 계좌는 빈 결과로 숨기지 않고 `failed` 에 남긴다(전체 검토 W3 리뷰).
 * 모든 계좌가 실패하면 첫 오류를 그대로 던져 화면이 '오류' 로 보이게 한다. */
export async function mergeAccountRows<T extends { id: number }>(
  ids: string[],
  load: (id: string) => Promise<T[]>,
): Promise<AccountRows<T>> {
  const settled = await Promise.allSettled(ids.map((id) => load(id)));
  const rows: T[] = [];
  const failed: string[] = [];
  settled.forEach((s, i) => (s.status === "fulfilled" ? rows.push(...s.value) : failed.push(ids[i])));
  if (ids.length && failed.length === ids.length) {
    const first = settled.find((s): s is PromiseRejectedResult => s.status === "rejected");
    throw first?.reason instanceof Error ? first.reason : new Error(String(first?.reason));
  }
  rows.sort((a, b) => b.id - a.id);
  return { rows, failed };
}

/** 일부 계좌만 실패했을 때 보일 문구. 없으면 null. */
export function partialText(failed: string[], what: string): string | null {
  return failed.length ? `${failed.join(", ")} 계좌의 ${what}을 읽지 못해 합계에서 빠졌어요` : null;
}
