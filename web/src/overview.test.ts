import { describe, expect, it } from "vitest";
import { mergeAccountRows, mergeHoldings, missingText, overviewRows, partialText, planCell, planCells } from "./overview";
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

describe("mergeHoldings (전체 검토 W4)", () => {
  it("같은 시장·종목은 수량·평가를 합치고 계좌를 모은다", () => {
    const p = (qty: number, ev: string, acct: string) => ({ ticker: "005930", market: "KR", qty, avg_price: "1", current_price: "1", eval_amount: ev, accounts: [acct] });
    const [m] = mergeHoldings([p(10, "100", "paper-main"), p(5, "50", "real-main")]);
    expect(m).toMatchObject({ qty: 15, eval_amount: "150", accounts: ["paper-main", "real-main"] });
  });
});

describe("mergeAccountRows", () => {
  it("성공한 계좌 행은 합치고 실패 계좌는 failed 로 남긴다", async () => {
    const r = await mergeAccountRows(["a", "b", "c"], (id) =>
      id === "b" ? Promise.reject(new Error("503")) : Promise.resolve([{ id: id === "a" ? 1 : 5 }]),
    );
    expect(r.rows.map((x) => x.id)).toEqual([5, 1]);
    expect(r.failed).toEqual(["b"]);
    expect(partialText(r.failed, "주문")).toContain("b 계좌의 주문");
    expect(partialText([], "주문")).toBeNull();
  });

  it("모든 계좌가 실패하면 오류를 던진다(빈 원장으로 보이지 않게)", async () => {
    await expect(mergeAccountRows(["a"], () => Promise.reject(new Error("HTTP 500")))).rejects.toThrow("HTTP 500");
  });
});
