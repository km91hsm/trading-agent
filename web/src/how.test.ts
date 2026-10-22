import { describe, expect, it } from "vitest";
import { HOW, HOW_IDS, sectionForStage } from "./how";
import { pipelineStages, type StageKey } from "./pipeline";

const ALL_STAGES: StageKey[] = [
  "00_inputs", "05_quant", "10_analyst", "20_debate", "25_research_summary", "30_decision", "31_decision",
  "35_calibration", "40_risk_check", "41_risk_check", "45_stop_loss", "50_order", "51_order", "55_order_sl",
];

describe("how", () => {
  it("절 id 는 고유하고 비어 있지 않다", () => {
    expect(new Set(HOW_IDS).size).toBe(HOW.length);
    for (const s of HOW) expect(s.steps.length).toBeGreaterThan(0);
  });
  it("모든 파이프라인 단계가 어느 절엔가 속한다", () => {
    for (const k of ALL_STAGES) expect(sectionForStage(k), k).not.toBeNull();
    // pipelineStages 가 내는 키도 전부 덮는다
    const files = ["00_inputs", "10_analyst_technical", "20_debate_r0_bull", "25_research_summary", "30_decision", "40_risk_check_paper-main", "50_order_paper-main", "31_decision_r1", "45_stop_loss_paper-main"];
    for (const st of pipelineStages(files)) expect(sectionForStage(st.key)).not.toBeNull();
  });
  it("모델 키는 settings.models 의 키만 쓴다", () => {
    const allowed = new Set(["orchestrator", "screener", "analyst", "researcher_bull", "researcher_bear", "trader", "reflector"]);
    for (const s of HOW) for (const st of s.steps) if (st.model) expect(allowed.has(st.model), st.model).toBe(true);
  });
});
