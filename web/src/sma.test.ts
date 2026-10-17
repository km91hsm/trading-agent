import { describe, expect, it } from "vitest";
import type { Time } from "lightweight-charts";
import { sma } from "./pages/Chart";

describe("sma", () => {
  it("n 번째 봉부터 평균을 낸다", () => {
    const closes = [1, 2, 3, 4].map((c, i) => ({ time: `2026-01-0${i + 1}` as Time, close: c }));
    expect(sma(closes, 2)).toEqual([
      { time: "2026-01-02", value: 1.5 },
      { time: "2026-01-03", value: 2.5 },
      { time: "2026-01-04", value: 3.5 },
    ]);
    expect(sma(closes, 5)).toEqual([]);
  });
});
