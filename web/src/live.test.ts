import { describe, expect, it } from "vitest";
import { applyQuotes, CLOSED_RECHECK_MS, LIVE_INTERVAL_MS, liveLabel, livePlan, nextDelay, STREAM_INTERVAL_MS } from "./live";
import { planCells } from "./overview";
import type { Position } from "./types";

const pos = (ticker: string, qty: number, avg: string, cur: string): Position => ({
  ticker,
  market: "KR",
  qty,
  avg_price: avg,
  current_price: cur,
  eval_amount: String(qty * Number(cur)),
});

const plan = {
  decision_id: 1, date: "2026-10-26", action: "BUY", target_weight: 0.1,
  take_profit: 120, stop_loss: 90, limit_price: 100, horizon_days: 10, horizon: "short" as const,
  dist_take_profit_pct: 0.2, dist_stop_pct: -0.1, dist_limit_pct: 0,
};

describe("livePlan (전체 검토 W8)", () => {
  it("실시간 현재가로 익절·손절·지정가 거리를 다시 잰다", () => {
    const p = livePlan(plan, 110)!;
    expect(p.dist_take_profit_pct).toBeCloseTo(120 / 110 - 1);
    expect(p.dist_stop_pct).toBeCloseTo(90 / 110 - 1);
    expect(p.dist_limit_pct).toBeCloseTo(100 / 110 - 1);
    expect(plan.dist_stop_pct).toBe(-0.1); // 원본 불변
  });
  it("가격이 없거나 0 이하·목표가 없으면 그 거리는 null", () => {
    expect(livePlan({ ...plan, stop_loss: null }, 110)!.dist_stop_pct).toBeNull();
    expect(livePlan(plan, 0)).toEqual(plan);
    expect(livePlan(null, 110)).toBeNull();
  });
  it("applyQuotes 가 실시간 행의 계획 거리를 갱신해 planCells 문구가 바뀐다", () => {
    const r = applyQuotes([{ ...pos("005930", 1, "100", "100"), plan }], {
      "005930": { price: "95", change_pct: null, asof: "x", stale: false },
    });
    expect(r.positions[0].plan!.dist_stop_pct).toBeCloseTo(90 / 95 - 1);
    expect(planCells(r.positions[0].plan, "KR")!.stop).not.toEqual(planCells(plan, "KR")!.stop);
  });
});

describe("applyQuotes", () => {
  it("실시간 값으로 평가·손익을 다시 계산하고 NAV 는 브로커 NAV + 평가 변화분, stale 는 무시", () => {
    const r = applyQuotes([pos("005930", 2, "100", "100"), pos("000660", 1, "50", "60")], {
      "005930": { price: "110", change_pct: 0.01, asof: "x", stale: false },
      "000660": { price: "999", change_pct: null, asof: "x", stale: true },
    }, "1260");
    expect(r.positions[0]).toMatchObject({ current_price: "110", eval_amount: "220", live: true });
    expect(r.positions[1]).toMatchObject({ current_price: "60", live: false });
    // 미결제 매수대금이 현금에 남은 KR 계좌: 현금+평가로 다시 만들지 않고 NAV 1260 에 +20 만
    expect(r.nav).toBe(1260 + 20);
    expect(r.pnl).toBe(220 + 60 - 250);
    expect(r.liveCount).toBe(1);
  });
  it("시세가 없으면 스냅샷 그대로", () => {
    const r = applyQuotes([pos("005930", 1, "100", "100")], null, null);
    expect(r.nav).toBeNull();
    expect(r.positions[0].live).toBe(false);
  });
});

describe("폴링 주기·표기", () => {
  it("장중 10초(웹소켓이면 1초), 장 밖 1분", () => {
    expect(nextDelay({ session: "open", asof: "", quotes: {} })).toBe(LIVE_INTERVAL_MS);
    expect(nextDelay({ session: "open", asof: "", quotes: {}, stream: true })).toBe(STREAM_INTERVAL_MS);
    expect(nextDelay({ session: "closed", asof: "", quotes: {}, stream: true })).toBe(CLOSED_RECHECK_MS);
    expect(liveLabel({ session: "open", asof: "", quotes: {}, stream: true }, 3)).toBe("실시간 3종목 · 체결 즉시");
    expect(nextDelay({ session: "closed", asof: "", quotes: {} })).toBe(CLOSED_RECHECK_MS);
    expect(nextDelay(null)).toBe(CLOSED_RECHECK_MS);
    expect(liveLabel({ session: "closed", asof: "", quotes: {} }, 0)).toBe("장 밖 · 마지막 잔고 기준");
    // 2-P13: 허브 양보 사유
    expect(liveLabel({ session: "open", asof: "", quotes: {}, hub_yield: "fills_listener" }, 2)).toBe(
      "실시간 2종목 · 10초마다 (실전 체결통보가 앱키 사용 중)",
    );
    expect(liveLabel({ session: "open", asof: "", quotes: {}, hub_yield: "appkey_in_use" }, 2)).toBe(
      "실시간 2종목 · 10초마다 (다른 연결이 앱키 사용 중)",
    );
    expect(liveLabel({ session: "open", asof: "", quotes: {} }, 2)).toBe("실시간 2종목 · 10초마다");
  });
});

import { bumpsGlobalTick } from "./hooks";

describe("bumpsGlobalTick", () => {
  it("체크포인트·하트비트만 바뀌면 전역 재조회를 하지 않는다", () => {
    expect(bumpsGlobalTick(["checkpoints"], false)).toBe(false);
    expect(bumpsGlobalTick(["daemon", "checkpoints"], false)).toBe(false);
    expect(bumpsGlobalTick(["positions"], false)).toBe(true);
    expect(bumpsGlobalTick([], true)).toBe(true);
  });
});

import { deferWhileHidden } from "./hooks";

describe("deferWhileHidden", () => {
  it("가려진 탭은 첫 읽기 뒤 SSE 변경에도 다시 읽지 않는다(보이면 한 번)", () => {
    expect(deferWhileHidden(true, false)).toBe(false); // 처음 마운트는 읽는다
    expect(deferWhileHidden(true, true)).toBe(true);
    expect(deferWhileHidden(false, true)).toBe(false);
  });
});

import { chunk, mergeQuotes } from "./hooks";

describe("시세 나눠 받기", () => {
  it("20개씩 나누고 응답을 합친다(하나라도 열렸으면 open)", () => {
    expect(chunk(Array.from({ length: 45 }, (_, i) => i), 20).map((c) => c.length)).toEqual([20, 20, 5]);
    const m = mergeQuotes([
      { session: "closed", asof: "a", quotes: { A: { price: "1", change_pct: null, asof: "", stale: true } } },
      { session: "open", asof: "b", quotes: { B: { price: "2", change_pct: 1, asof: "", stale: false } } },
    ]);
    expect(m.session).toBe("open");
    expect(m.stream).toBe(false);
    expect(Object.keys(m.quotes).sort()).toEqual(["A", "B"]);
  });
});


describe("degradeQuotes (2-W1)", () => {
  it("요청 실패·TTL 초과면 모든 시세를 stale 로 내리고 표 머리에 지연을 적는다", async () => {
    const { degradeQuotes } = await import("./live");
    const r = degradeQuotes({
      session: "open",
      asof: "2026-10-28T00:10:00+00:00",
      quotes: { "005930": { price: "100", change_pct: 0, asof: "x", stale: false } },
      stream: true,
    });
    expect(r.quotes["005930"].stale).toBe(true);
    expect(r.degraded).toBe(true);
    expect(applyQuotes([pos("005930", 1, "90", "90")], r.quotes).liveCount).toBe(0);
    expect(liveLabel(r, 0)).toContain("지연");
    expect(liveLabel(r, 0)).toContain("09:10"); // KST 표기
  });
});
