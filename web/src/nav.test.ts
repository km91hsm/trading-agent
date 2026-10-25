import { describe, expect, it } from "vitest";
import { chordTarget, flatNav, MOBILE_TABS, NAV, paletteItems, recentFeatures } from "./nav";

describe("nav (메뉴 한 곳)", () => {
  it("묶음 셋·경로 중복 없음·모바일 탭 4칸", () => {
    expect(NAV.map((g) => g.id)).toEqual(["ops", "analysis", "system"]);
    const paths = flatNav().map((i) => i.to);
    expect(new Set(paths).size).toBe(paths.length);
    expect(MOBILE_TABS.map((t) => t.to)).toEqual(["/", "/today", "/orders", "/account"]);
    expect(recentFeatures(2).length).toBe(2);
  });
  it("팔레트: 화면 검색·종목 코드·이름 검색, 이동 전용", () => {
    expect(paletteItems("").length).toBe(flatNav().length);
    expect(paletteItems("주문")[0]).toMatchObject({ kind: "page", to: "/orders" });
    expect(paletteItems("005930", { "KR:005930": "삼성전자" })[0]).toEqual({ kind: "ticker", label: "삼성전자 차트", hint: "KR 005930", to: "/chart?market=KR&ticker=005930" });
    expect(paletteItems("aapl")[0]).toMatchObject({ kind: "ticker", to: "/chart?market=US&ticker=AAPL" });
    const byName = paletteItems("삼성", { "KR:005930": "삼성전자" });
    expect(byName.some((i) => i.to.endsWith("ticker=005930"))).toBe(true);
    expect(paletteItems("zzzz-nothing").filter((i) => i.kind === "page")).toEqual([]);
  });
  it("g 두 키", () => {
    expect(chordTarget("o")).toBe("/orders");
    expect(chordTarget("D")).toBe("/");
    expect(chordTarget("z")).toBeNull();
  });
});
