import { describe, expect, it } from "vitest";
import { accountSubRows, liveSummary, mergeAccountRows, mergeHoldings, missingText, overviewRows, partialText, planCell, planCells, stopCell } from "./overview";
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

describe("stopCell — 실제 적용 손절선", () => {
  it("결정 손절가는 꼬리표 없이, 바닥·기본값은 출처 꼬리표를 단다", () => {
    expect(stopCell({ level: 95, source: "decision", dist_pct: -0.05 }, "KR")).toEqual({ text: "95원 (-5.0%)", term: null, cls: "dn" });
    expect(stopCell({ level: 92, source: "floor", dist_pct: -0.08 }, "KR")).toMatchObject({ term: "stop.floor" });
    expect(stopCell({ level: 85, source: "default", dist_pct: -0.15 }, "KR")).toMatchObject({ term: "stop.default", text: "85원 (-15.0%)" });
  });
  it("손절선이 없으면 null", () => {
    expect(stopCell(null, "KR")).toBeNull();
    expect(stopCell(undefined, "KR")).toBeNull();
  });
  it("mergeHoldings 는 더 낮은(늦게 걸리는) 손절선을 남긴다", () => {
    const p = (lvl: number, acct: string) => ({ ticker: "005930", market: "KR", qty: 1, avg_price: "1", current_price: "1", eval_amount: "1", accounts: [acct], stop: { level: lvl, source: "decision" as const, dist_pct: null } });
    expect(mergeHoldings([p(95, "paper-main"), p(90, "real-main")])[0].stop!.level).toBe(90);
    expect(mergeHoldings([p(90, "paper-main"), p(95, "real-main")])[0].stop!.level).toBe(90);
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


describe("liveSummary", () => {
  const e = {
    nav: "1000000",
    cash: "500000",
    pnl: "0",
    pnl_pct: 0,
    positions: [{ ticker: "005930", qty: 10, avg_price: "50000", current_price: "50000", eval_amount: "500000" }],
    sectors: [],
    unmapped: [],
    accounts: [],
  } as unknown as Parameters<typeof liveSummary>[0];
  it("실시간 시세로 순자산·평가손익·손익률이 움직인다", () => {
    const s = liveSummary(e, "KR", { "005930": { price: "55000", change_pct: 1, asof: "", stale: false } });
    expect(s.live).toBe(true);
    expect(s.nav).toContain("1,050,000");
    expect(s.pnl).toContain("50,000");
    expect(s.pnlPct).toBeCloseTo(0.1);
  });
  it("실시간 값이 없거나 stale 이면 서버 값 그대로", () => {
    expect(liveSummary(e, "KR", null).live).toBe(false);
    expect(liveSummary(e, "KR", { "005930": { price: "55000", change_pct: 1, asof: "", stale: true } }).nav).toContain("1,000,000");
  });
});

describe("계좌별 펼침 줄(계좌 여러 개)", () => {
  it("한 시장·환경에 계좌가 둘 이상이면 계좌마다 순자산·현금·기준일, 하나면 없음", () => {
    const o: Overview = {
      markets: { KR: { envs: { real: env("3000", ["real-main", "real-2", "real-3"]), paper: env("1000", ["paper-main"]) } } },
      accounts_missing_snapshot: {}, pending_approvals: 0, configured: 4,
    } as Overview;
    const rows = overviewRows(o);
    const real = rows.find((r) => r.env === "real")!;
    expect(accountSubRows(real)).toEqual([
      { id: "real-main", nav: "3,000원", cash: "3,000원", date: "2026-10-24" },
      { id: "real-2", nav: "3,000원", cash: "3,000원", date: "2026-10-24" },
      { id: "real-3", nav: "3,000원", cash: "3,000원", date: "2026-10-24" },
    ]);
    expect(accountSubRows(rows.find((r) => r.env === "paper")!)).toEqual([]);
  });
});

