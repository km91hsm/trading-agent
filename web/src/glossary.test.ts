import { describe, expect, it } from "vitest";
import { GLOSSARY, termHelp, termLabel } from "./glossary";

const REQUIRED = [
  "action.BUY",
  "action.SELL",
  "action.HOLD",
  "action.ADD",
  "action.TRIM",
  "target_weight",
  "stop_loss",
  "take_profit",
  "confidence",
  "stance",
  "stance.normal",
  "stance.conservative",
  "mode.auto",
  "mode.approve",
  "mode.off",
  "dry_run",
  "trigger",
  "sycophantic",
  "stage.00_inputs",
  "stage.05_quant",
  "stage.10_analyst",
  "stage.20_debate",
  "stage.25_research_summary",
  "stage.30_decision",
  "stage.31_decision",
  "stage.35_calibration",
  "stage.40_risk_check",
  "stage.41_risk_check",
  "stage.45_stop_loss",
  "stage.50_order",
  "stage.51_order",
  "stage.55_order_sl",
];

describe("glossary", () => {
  it.each(REQUIRED)("%s 가 라벨·설명을 가진다", (k) => {
    expect(GLOSSARY[k]?.label).toBeTruthy();
    expect(GLOSSARY[k]?.help).toBeTruthy();
  });
  it("termLabel 은 라벨, 없으면 키 그대로", () => {
    expect(termLabel("stop_loss")).toBe("손절선");
    expect(termLabel("action.HOLD")).toBe("보유");
    expect(termLabel("nope")).toBe("nope");
    expect(termHelp("nope")).toBe("");
  });
});
