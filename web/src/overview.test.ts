import { describe, expect, it } from "vitest";
import { missingText, overviewRows, planCell, planCells } from "./overview";
import type { Overview, OverviewEnv } from "./types";

const env = (nav: string, accounts: string[]): OverviewEnv => ({
  nav,
  cash: nav,
  pnl: "0",
  pnl_pct: null,
  positions: [],
  sectors: [],
  unmapped: [],
  accounts: accounts.map((id) => ({ id, env: "paper", nav, cash: nav, snapshot_date: "2026-10-24" })),
});

describe("overview", () => {
  it("시장 × 모의/실전 행, 통화·환경 섞임 없음", () => {
    const o: Overview = {
      markets: { KR: { envs: { paper: env("1000", ["paper-main"]), real: env("500", ["real-main"]) } }, US: { envs: { paper: env("1000", ["paper-main"]) } } },
      accounts_missing_snapshot: { KR: [], US: ["real-main"] },
      pending_approvals: 0,
      configured: 2,
    };
    expect(overviewRows(o).map((r) => [r.market, r.env, r.nav])).toEqual([
      ["KR", "paper", "1,000원"],
      ["KR", "real", "500원"],
      ["US", "paper", "$1,000.00"],
    ]);
    expect(missingText(o)).toBe("US: real-main");
    expect(overviewRows(null)).toEqual([]);
  });

  it("스냅샷이 하나도 없으면 행은 없고 미확인 목록만", () => {
    const o: Overview = { markets: { KR: { envs: {} } }, accounts_missing_snapshot: { KR: ["a", "b"] }, pending_approvals: 0, configured: 2 };
    expect(overviewRows(o)).toEqual([]);
    expect(missingText(o)).toBe("KR: a, b");
  });

  it("계획 셀: 가격(거리)", () => {
    expect(planCell(110000, 0.1, "KR")).toBe("110,000원 (+10.0%)");
    expect(planCell(95, -0.05, "US")).toBe("$95.00 (-5.0%)");
    expect(planCell(null, 0.1, "KR")).toBe("—");
    const c = planCells(
      { decision_id: 1, date: "2026-10-24", action: "BUY", target_weight: 0.2, take_profit: 110, stop_loss: 95, limit_price: null, horizon_days: 5, horizon: "short", dist_take_profit_pct: 0.1, dist_stop_pct: -0.05, dist_limit_pct: null },
      "US",
    );
    expect(c).toMatchObject({ target: "20%", limit: "—", tpClass: "up", stopClass: "dn" });
    expect(planCells(null, "KR")).toBeNull();
  });
});
