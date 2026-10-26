import { describe, expect, it } from "vitest";
import { genericBlocks, settingsBlocks, stageReport } from "./report";

describe("stageReport", () => {
  it("애널리스트: 신호·확신·근거 2개", () => {
    const b = stageReport("10_analyst_technical", {
      role: "technical",
      summary: "추세 상승",
      bullets: ["20일선 위"],
      signal: "BUY",
      score: 0.4,
      confidence: 0.7,
      horizon: "short",
      key_risks: ["거래량 감소"],
      counter_evidence: [],
      evidence: [{ claim: "a", source_id: "x" }, { claim: "b", source_id: "y" }, { claim: "c", source_id: "z" }],
      data_gaps: [],
    });
    expect(b[0].title).toBe("차트 애널리스트");
    expect(b[0].rows).toContainEqual(["신호", "매수"]);
    expect(b[0].rows).toContainEqual(["확신", "0.70"]);
    expect(b.find((x) => x.title === "근거")?.list).toEqual(["a", "b"]);
  });

  it("토론: 입장·논거·반박", () => {
    const b = stageReport("20_debate_r1_bear", {
      side: "bear",
      round: 1,
      stance: "BEARISH",
      valuation_view: "OVERVALUED",
      arguments: ["PER 과열"],
      rebuttals: ["성장 둔화"],
      timing: { action: "WAIT", condition: null, price_zone: null },
      conviction: 0.6,
      what_would_change_my_mind: [],
    });
    expect(b[0].title).toBe("약세 리서처 · 라운드 1");
    expect(b[0].rows).toContainEqual(["입장", "약세"]);
    expect(b[0].rows).toContainEqual(["타이밍", "대기"]);
    expect(b.map((x) => x.title)).toEqual(["약세 리서처 · 라운드 1", "논거", "반박"]);
  });

  it("결정: 행동·비중·가격·근거·무효화 조건", () => {
    const b = stageReport("30_decision", {
      action: "BUY",
      target_weight: 0.15,
      order_type: "LIMIT",
      limit_price: 70000,
      stop_loss: 65000,
      take_profit: 80000,
      horizon_days: 10,
      confidence: 0.62,
      rationale: "실적 개선",
      invalidation_conditions: ["65,000 이탈"],
    });
    expect(b[0].lead).toBe("매수 · 목표 비중 15% · 확신 0.62");
    expect(b[0].rows).toContainEqual(["주문 방식", "지정가 70,000"]);
    expect(b[0].rows).toContainEqual(["보유 기간", "10일"]);
    expect(b[0].paragraphs).toEqual(["실적 개선"]);
    expect(b[1]).toEqual({ title: "무효화 조건", list: ["65,000 이탈"] });
  });

  it("게이트: 계좌·거부 사유 전부", () => {
    const b = stageReport("40_risk_check_paper-main", { account_id: "paper-main", approved: false, adjusted_qty: 0, reasons: ["a", "b"], mode: "auto", dry_run: true });
    expect(b[0].title).toBe("리스크 게이트 · paper-main");
    expect(b[0].lead).toBe("거부");
    expect(b[0].list).toEqual(["a", "b"]);
  });

  it("주문: 상태·요청·접수번호", () => {
    const b = stageReport("50_order_paper-main", { request: { side: "BUY", qty: 3, order_type: "LIMIT", price: "70000" }, result: { order_no: "123", message: "정상" }, mode: "auto", status: "accepted" });
    expect(b[0].title).toBe("주문 · paper-main");
    expect(b[0].lead).toBe("접수");
    expect(b[0].rows).toContainEqual(["요청", "BUY 3주 @70,000"]);
    expect(b[0].rows).toContainEqual(["접수번호", "123"]);
  });

  it("입력: 현재가·오류 목록", () => {
    const b = stageReport("00_inputs", {
      meta: { name: "삼성전자", ticker: "005930", last_candle_date: "2026-10-22" },
      technical: { quote: { data: { price: "214500", change_pct: -0.0403 } } },
      errors: ["news_macro.news: 401"],
    });
    expect(b[0].lead).toBe("삼성전자 · 현재가 214,500 (-4.03%) · 마지막 봉 2026-10-22");
    expect(b[0].list).toEqual(["news_macro.news: 401"]);
  });

  it("시장 단계: 전략가 제안", () => {
    const b = stageReport("_market_view", { stance: "RISK_OFF", hedge_weight: 0.1, confidence: 0.8, horizon_days: 5, rationale: "변동성 확대", invalidation_conditions: [] });
    expect(b[0].lead).toBe("위험 회피 · 헤지 비중 10% · 확신 0.80");
  });

  it("모르는 단계는 최상위 값 표, 빈 값은 []", () => {
    expect(stageReport("99_x", { a: 1, b: [1, 2], c: { d: 1 } })[0].rows).toEqual([
      ["a", "1"],
      ["b", "2개 항목"],
      ["c", "항목 1개"],
    ]);
    expect(stageReport("30_decision", null)).toEqual([]);
    expect(genericBlocks({})).toEqual([]);
  });

  it("필드가 빠져도 던지지 않는다", () => {
    for (const s of ["00_inputs", "05_quant", "10_analyst_x", "20_debate_r0_bull", "25_research_summary", "30_decision", "31_decision_r1", "35_calibration", "40_risk_check_a", "50_order_a", "_market_view", "_scan", "_screen", "_discovery"]) {
      expect(() => stageReport(s, { x: 1 })).not.toThrow();
    }
  });
});

describe("settingsBlocks", () => {
  it("절마다 블록, 스칼라는 '기본'", () => {
    const b = settingsBlocks({ debate_rounds: 1, risk: { max_position_pct: 0.25, horizon_profiles: { short: { max_days: 10 } } }, markets: ["KR", "US"] });
    expect(b[0]).toEqual({ title: "기본", rows: [["debate_rounds", "1"], ["markets", "KR, US"]] });
    expect(b[1].title).toBe("리스크 한도 (risk)");
    expect(b[1].rows).toContainEqual(["horizon_profiles", "short=…"]);
  });
});
