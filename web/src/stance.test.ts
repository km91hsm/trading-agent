import { describe, expect, it } from "vitest";
import { stanceText } from "./stance";

describe("stanceText", () => {
  it("계좌 시장 순서대로 시장별 성향을 이어 붙인다", () => {
    expect(
      stanceText({ markets: ["KR", "US"], stance_by_market: { KR: "normal", US: "conservative" } }),
    ).toBe("KR normal · US conservative");
  });
  it("계좌에 없는 시장은 표시하지 않고, 값이 없으면 - 로 둔다", () => {
    expect(stanceText({ markets: ["KR"], stance_by_market: { KR: "normal", US: "normal" } })).toBe("KR normal");
    expect(stanceText({ markets: ["US"], stance_by_market: {} })).toBe("US -");
  });
});
