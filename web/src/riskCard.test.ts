import { describe, expect, it } from "vitest";
import { riskSummary, topCorrelation } from "./riskCard";

describe("위험 카드", () => {
  it("요약 3줄과 최고 상관 쌍", () => {
    const r = {
      var95: 0.0123,
      cvar95: 0.0201,
      beta: 1.234,
      corr: { tickers: ["A", "B", "C"], matrix: [[1, 0.9, 0.1], [0.9, 1, 0.3], [0.1, 0.3, 1]] },
    };
    expect(riskSummary(r).map((x) => x.value)).toEqual(["1.23%", "2.01%", "1.23"]);
    expect(topCorrelation(r)).toEqual({ a: "A", b: "B", rho: 0.9 });
  });
  it("부족하면 비어 있다", () => {
    expect(riskSummary({ insufficient: "잔고 스냅샷 없음" })).toEqual([]);
    expect(topCorrelation({ corr: { tickers: ["A"], matrix: [[1]] } })).toBeNull();
  });
});
