import { describe, expect, it } from "vitest";
import { pipelineStages, stageSummary } from "./pipeline";

describe("pipelineStages", () => {
  it("기본 판 9개를 순서대로, 파일을 묶고 done 을 표시한다", () => {
    const st = pipelineStages(["40_risk_check_paper-main", "10_analyst_technical", "00_inputs", "10_analyst_fundamental", "30_decision"]);
    expect(st.map((s) => s.key)).toEqual([
      "00_inputs",
      "05_quant",
      "10_analyst",
      "20_debate",
      "25_research_summary",
      "30_decision",
      "40_risk_check",
      "50_order",
    ]);
    expect(st.map((s) => s.done)).toEqual([true, false, true, false, false, true, true, false]);
    expect(st[2].files).toEqual(["10_analyst_fundamental", "10_analyst_technical"]);
    expect(st[6].files).toEqual(["40_risk_check_paper-main"]);
    expect(st[0].label).toBe("입력");
  });
  it("재평가·손절·보정 판은 파일이 있을 때만 붙는다", () => {
    const st = pipelineStages(["00_inputs", "31_decision_r1", "41_risk_check_r1_paper-main", "45_stop_loss_paper-main", "35_calibration"]);
    const keys = st.map((s) => s.key);
    expect(keys).toContain("31_decision");
    expect(keys).toContain("41_risk_check");
    expect(keys).toContain("45_stop_loss");
    expect(keys).toContain("35_calibration");
    expect(keys).not.toContain("51_order");
    expect(st.find((s) => s.key === "31_decision")?.optional).toBe(true);
  });
});

describe("stageSummary", () => {
  it("요약 붕괴는 r0 우선", () => {
    expect(stageSummary("25_research_summary", [{ file: "25_research_summary", data: { sycophantic: true } }])).toBe("붕괴 · r0 우선");
    expect(stageSummary("25_research_summary", [{ file: "25_research_summary", data: { sycophantic: false, preferred: "final" } }])).toBe("정상 수렴 · final");
  });
  it("애널리스트는 입장 집계, 결정은 action·비중·확신", () => {
    expect(
      stageSummary("10_analyst", [
        { file: "10_analyst_technical", data: { stance: "BULLISH" } },
        { file: "10_analyst_fundamental", data: { stance: "bearish" } },
        { file: "10_analyst_sentiment", data: { stance: "NEUTRAL" } },
      ]),
    ).toBe("강세 1 · 중립 1 · 약세 1");
    expect(stageSummary("30_decision", [{ file: "30_decision", data: { action: "ADD", target_weight: 0.15, confidence: 0.72 } }])).toBe(
      "추가 매수 · 비중 15% · 확신 0.72",
    );
  });
  it("게이트·주문은 계좌별로", () => {
    expect(
      stageSummary("40_risk_check", [
        { file: "40_risk_check_paper-main", data: { verdict: { approved: true, adjusted_qty: 12 } } },
        { file: "40_risk_check_real-main", data: { verdict: { approved: false, reasons: ["브로커 없음"] } } },
      ]),
    ).toBe("paper-main: 승인 12주 / real-main: 거부 · 브로커 없음");
    expect(stageSummary("50_order", [{ file: "50_order_paper-main", data: { status: "accepted" } }])).toBe("paper-main: 접수");
    expect(stageSummary("00_inputs", [{ file: "00_inputs", data: { errors: { "a.b": "x" } } }])).toBe("오류 절 1건");
  });
});
