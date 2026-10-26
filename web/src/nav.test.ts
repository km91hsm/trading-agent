import { describe, expect, it } from "vitest";
import { chartPath, chordTarget, flatNav, MOBILE_TABS, NAV, paletteItems, parseChartQuery, recentFeatures } from "./nav";

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

describe("종목 검색 연결 (Phase 24)", () => {
  it("서버 검색 결과가 차트 이동 후보가 된다(보유 표시, US 거래소)", () => {
    const items = paletteItems("삼성", {}, [
      { market: "KR", ticker: "009150", name: "삼성전기", exchange: null, source: "held" },
      { market: "US", ticker: "SSNLF", name: "Samsung", exchange: "NYS", source: "listed" },
    ]);
    const t = items.filter((i) => i.kind === "ticker");
    expect(t[0]).toMatchObject({ label: "삼성전기 차트", hint: "KR 009150 · 보유", to: "/chart?market=KR&ticker=009150" });
    expect(t[1].to).toBe("/chart?market=US&ticker=SSNLF&exchange=NYS");
  });
  it("정확한 코드 항목은 검색 결과의 거래소로 채운다", () => {
    const items = paletteItems("JPM", {}, [{ market: "US", ticker: "JPM", name: "JPMorgan", exchange: "NYS", source: "universe" }]);
    const t = items.filter((i) => i.kind === "ticker");
    expect(t).toHaveLength(1);
    expect(t[0]).toMatchObject({ to: "/chart?market=US&ticker=JPM&exchange=NYS", label: "JPMorgan 차트" });
  });
  it("차트 URL 질의를 읽는다", () => {
    expect(parseChartQuery(new URLSearchParams("market=US&ticker=TSLA&exchange=NYS"))).toEqual({ ticker: "TSLA", market: "US", exchange: "NYS" });
    expect(parseChartQuery(new URLSearchParams(""))).toEqual({ ticker: "005930", market: "KR", exchange: "NAS" });
    expect(chartPath("KR", "005930")).toBe("/chart?market=KR&ticker=005930");
  });
});
