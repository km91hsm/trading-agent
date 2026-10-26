import { describe, expect, it } from "vitest";
import { shadowRows, verdictOf } from "./shadow";

const s = (n: number, lo: number | null, hi: number | null) => ({ n, mean_diff: lo, ci_low: lo, ci_high: hi });

describe("shadow", () => {
  it("판정: 쌍 부족·우위·열위·구분 안 됨", () => {
    expect(verdictOf(s(5, 0.01, 0.02), 20)).toBe("부족");
    expect(verdictOf(s(25, 0.001, 0.02), 20)).toBe("우위");
    expect(verdictOf(s(25, -0.02, -0.001), 20)).toBe("열위");
    expect(verdictOf(s(25, -0.01, 0.01), 20)).toBe("구분 안 됨");
    expect(verdictOf(s(0, null, null), 20)).toBe("부족");
  });

  it("행: 두 기준을 % 로", () => {
    const rows = shadowRows({
      market: "KR",
      min_pairs: 20,
      pairs: { candidates_1n: s(25, 0.004, 0.01), quant_topn: s(0, null, null) },
      recent: [],
    });
    expect(rows.map((r) => r.key)).toEqual(["candidates_1n", "quant_topn"]);
    expect(rows[0].lowPct).toBeCloseTo(0.4);
    expect(rows[0].verdict).toBe("우위");
    expect(rows[1].meanPct).toBeNull();
    const withIndex = shadowRows({
      market: "KR",
      min_pairs: 20,
      pairs: {
        candidates_1n: s(0, null, null),
        quant_topn: s(0, null, null),
        index_hold: s(25, -0.02, -0.001),
      },
      recent: [],
    });
    expect(withIndex.map((r) => r.key)).toEqual(["candidates_1n", "quant_topn", "index_hold"]);
    expect(withIndex[2].label).toBe("지수 보유");
    expect(withIndex[2].verdict).toBe("열위");
  });
});
