import { describe, expect, it } from "vitest";
import { overviewRows, planCell, planCells } from "./overview";
import type { Overview, OverviewMarket } from "./types";

const env = (nav: string, accounts: string[]) => ({ nav, cash: nav, pnl: "0", pnl_pct: null, accounts });
const mk = (nav: string, by_env: OverviewMarket["by_env"] = { paper: env(nav, ["paper-main"]) }) => ({ nav, cash: "0", pnl: "0", pnl_pct: null, positions: [], sectors: [], unmapped: [], accounts: [], by_env });

describe("overview", () => {
  it("모의·실전은 다른 행", () => {
    const o: Overview = { markets: { KR: mk("1500", { paper: env("1000", ["paper-main"]), real: env("500", ["real-main"]) }) }, accounts_missing_snapshot: {}, pending_approvals: 0 };
    expect(overviewRows(o).map((r) => [r.env, r.nav])).toEqual([
      ["paper", "1,000원"],
      ["real", "500원"],
    ]);
  });

  it("시장별 행, 통화 섞임 없음", () => {
    const o: Overview = { markets: { KR: mk("1500000"), US: mk("1000") }, accounts_missing_snapshot: { KR: [], US: ["real-main"] }, pending_approvals: 0 };
    const rows = overviewRows(o);
    expect(rows.map((r) => [r.market, r.nav])).toEqual([
      ["KR", "1,500,000원"],
      ["US", "$1,000.00"],
    ]);
    expect(rows[1].missing).toEqual(["real-main"]);
    expect(overviewRows(null)).toEqual([]);
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
