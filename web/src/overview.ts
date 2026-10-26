// 전체 계좌 합산(/api/portfolio/overview)과 보유 행 계획 열 — 행 변환 순수 함수 (Phase 24).
import { fmtMoney } from "./format";
import type { Market, Overview, OverviewMarket, PositionPlan } from "./types";

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
  missing: string[];
  data: OverviewMarket;
};

/** 시장 × 모의/실전 머리 행. 통화가 달라 시장끼리, 가짜 돈과 진짜 돈이라 모의·실전끼리 더하지 않는다
 * (PRODUCT.md 원칙 3). 모의가 먼저. 보유 종목 수는 그 환경 계좌가 가진 합산 종목 수. */
export function overviewRows(o: Overview | null | undefined): OverviewRow[] {
  if (!o) return [];
  const out: OverviewRow[] = [];
  for (const m of ["KR", "US"] as Market[]) {
    const d = o.markets[m];
    if (!d) continue;
    for (const env of ["paper", "real"] as const) {
      const e = d.by_env?.[env];
      if (!e) continue;
      const mine = new Set(e.accounts);
      out.push({
        market: m,
        env,
        nav: fmtMoney(e.nav, m),
        cash: fmtMoney(e.cash, m),
        pnl: fmtMoney(e.pnl, m),
        pnlPct: e.pnl_pct,
        holdings: d.positions.filter((p) => (p.accounts ?? []).some((a) => mine.has(a))).length,
        accounts: e.accounts.length,
        missing: o.accounts_missing_snapshot[m] ?? [],
        data: d,
      });
    }
  }
  return out;
}
