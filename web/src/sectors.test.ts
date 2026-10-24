import { describe, expect, it } from "vitest";
import { capLinePct, regimeText, sectorRows, type RegimeResp } from "./sectors";

describe("섹터 비중", () => {
  it("행 변환: 비중·막대·상한 초과(현금·헤지 제외)", () => {
    const rows = sectorRows(
      [
        { sector: "반도체", eval_amount: "4500000", weight: 0.45, tickers: ["005930", "000660"], count: 2 },
        { sector: "헤지", eval_amount: "2000000", weight: 0.2, tickers: ["114800"], count: 1 },
        { sector: "현금", eval_amount: "3500000", weight: 0.35, tickers: [], count: 0 },
        { sector: "기타", eval_amount: "0", weight: null, tickers: [], count: 0 },
      ],
      0.4,
    );
    expect(rows[0]).toMatchObject({ pct: "45.0%", barPct: 45, over: true, capped: true, count: 2 });
    expect(rows[1].over).toBe(false);
    expect(rows[2]).toMatchObject({ capped: false, over: false });
    expect(rows[3]).toMatchObject({ pct: "—", barPct: 0 });
    expect(capLinePct(0.4)).toBe(40);
  });
});

describe("국면 칩", () => {
  const base: RegimeResp = { enabled: true, max_weight_pct: 0.2, leverage: true, bear_min_signals: 2, markets: [] };
  const m = (market: string, state: RegimeResp["markets"][number]["state"], count: number) => ({
    market, state, count, since: "2026-10-20", asof: "2026-10-24",
    signals: { ma_below: count >= 1, momentum_weak: count >= 2, vol_high: count >= 3 },
  });
  it("문구·수준", () => {
    expect(regimeText(null).text).toBe("확인 중");
    expect(regimeText({ ...base, enabled: false }).text).toBe("헤지 꺼짐");
    expect(regimeText({ ...base, markets: [m("KR", null, 0)] }).text).toBe("판정 전");
    const r = regimeText({ ...base, markets: [m("KR", "bear", 2), m("US", "bull", 0)] });
    expect(r.text).toBe("KR 하락 2/3 · US 상승 0/3");
    expect(r.level).toBe("err");
    expect(r.title).toContain("200일선 아래, 20일 수익률 약세");
    expect(regimeText({ ...base, markets: [m("KR", "neutral", 1)] }).level).toBe("warn");
  });
});
