import { describe, expect, it } from "vitest";
import { heatRows, themeName, type ThemeResp } from "./themeHeat";

describe("themeHeat", () => {
  it("이름·성분·종목·상위 표시", () => {
    const r: ThemeResp = {
      enabled: true,
      top_n: 1,
      market: "KR",
      date: "2026-10-26",
      observed_at: null,
      signals: {},
      themes: [
        { theme: "섹터:반도체", rank: 1, score: 0.812, components: { news: 1, rank: 0.4, flow: 0.1, saveticker: 0, vol: 0 }, catalyst_ok: true, tickers: [{ ticker: "005930", name: "삼성전자", why: [] }] },
        { theme: "kw:로봇", rank: 2, score: 0.2, components: { news: 0.2 }, catalyst_ok: false, tickers: [] },
      ],
    };
    const rows = heatRows(r);
    expect(rows[0]).toMatchObject({ name: "반도체 (섹터)", score: "0.81", catalyst: "있음", drivers: "새 뉴스·순위", tickers: "삼성전자(005930)", top: true });
    expect(rows[1]).toMatchObject({ name: "로봇 (뉴스 키워드)", drivers: "—", top: false, catalyst: "없음 (커뮤니티뿐)" });
    expect(themeName("x")).toBe("x");
    expect(heatRows(null)).toEqual([]);
  });
});
