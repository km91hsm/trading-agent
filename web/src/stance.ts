import type { Account } from "./types";

/** 계좌 성향을 시장별로 표시한다 (executor 가 시장별로 판정하므로 합산 값은 없다). */
export function stanceText(a: Pick<Account, "markets" | "stance_by_market">): string {
  return a.markets.map((m) => `${m} ${a.stance_by_market[m] ?? "-"}`).join(" · ");
}
