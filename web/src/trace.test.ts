import { describe, expect, it } from "vitest";
import { limitDistancePct, traceSteps, type TraceResp, unknownLine } from "./trace";

const T: TraceResp = {
  decision: { id: 1, date: "2026-10-09", market: "KR", ticker: "005930", action: "BUY", data: { target_weight: 0.1, confidence: 0.7, horizon_days: 10 } },
  quote: "70000",
  analysts: [{ role: "technical", label: "차트", signal: "BULLISH", confidence: 0.7, summary: "s", evidence: ["a", "b"] }],
  debate: [
    { round: 0, side: "bear", stance: "BEARISH", conviction: 0.4, timing: "WAIT" },
    { round: 0, side: "bull", stance: "BULLISH", conviction: 0.8, timing: "NOW" },
  ],
  research: { rounds: 1, gap_round0: 0.4, gap_final: 0.1, sycophantic: true, preferred: "round0" },
  gate: [{ stage: "40_risk_check_paper-main", account_id: "paper-main", approved: true, qty: 3, reasons: ["r"], note: "n" }],
  orders: [{ stage: "50_order_paper-main", account_id: "paper-main", status: "accepted", order_no: "1", side: "BUY", qty: 3, price: "70000" }],
  reevaluations: [{ round: "r1", decision: { action: "HOLD" }, gate: [{ stage: "41", account_id: "paper-main", approved: false, reasons: ["HOLD"] }], orders: [] }],
  stop_loss: { gate: [], orders: [] },
  reprice: { gate: [], orders: [] },
  stages: [],
};

describe("trace (결정 추적)", () => {
  it("단계별 줄", () => {
    const steps = traceSteps(T);
    expect(steps.map((s) => s.key)).toEqual(["analysts", "debate", "research", "decision", "gate", "orders", "re-r1"]);
    expect(steps[0].lines[0]).toBe("차트: 강세 · 확신 0.70 · a / b");
    expect(steps[1].lines[0]).toBe("r0: 약세 약세 0.40 · 강세 강세 0.80");
    expect(steps[2].lines[0]).toContain("붕괴");
    expect(steps[4].lines[0]).toBe("paper-main: 통과 3주 · r (n)");
    expect(steps[5].lines[0]).toBe("paper-main: 매수 3주 @70000 → 접수 #1");
    expect(steps[6].title).toBe("재평가 r1: HOLD");
  });
  it("지정가 거리·unknown 경고", () => {
    expect(limitDistancePct("70700", "70000")).toBeCloseTo(1, 5);
    expect(limitDistancePct(null, "70000")).toBeNull();
    expect(unknownLine([{ status: "accepted" }])).toBe("");
    expect(unknownLine([{ status: "unknown" }, { status: "unknown" }])).toContain("확인 필요 2건");
  });
});
