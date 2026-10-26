import { describe, expect, it } from "vitest";
import { GLOSSARY, sideLabel, termHelp, termLabel } from "./glossary";

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
    expect(termLabel("action.HOLD")).toBe("관망"); // 보유 수량과 겹치지 않게 (전체 검토 W12)
    expect(termLabel("nope")).toBe("nope");
    expect(termHelp("nope")).toBe("");
  });
});

describe("용어 충돌 (전체 검토 W12)", () => {
  it("게이트 통과·관망·주문 없이 판정은 사람 승인·보유·모의 계좌와 다른 말", () => {
    expect(termLabel("gate.pass")).toBe("통과");
    expect(termLabel("dry_run")).toBe("주문 없이 판정");
    expect(termLabel("gate.pass")).not.toBe(termLabel("mode.approve"));
    expect(termLabel("action.HOLD")).not.toBe("보유");
  });
  it("sideLabel 은 BUY/SELL 을 한글로, 모르는 값은 그대로, 빈 값은 빈 문자열", () => {
    expect(sideLabel("BUY")).toBe("매수");
    expect(sideLabel("SELL")).toBe("매도");
    expect(sideLabel("X")).toBe("X");
    expect(sideLabel(null)).toBe("");
  });
});
