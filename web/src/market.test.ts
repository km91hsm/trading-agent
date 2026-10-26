import { describe, expect, it } from "vitest";
import { bookRows, indexChips, marketPollMs } from "./market";

describe("지수 칩·호가 표", () => {
  it("지수: 장중만, 순서·부호·방향", () => {
    const r = {
      session: "open" as const,
      asof: "",
      indices: { KOSDAQ: { price: "870.5", change_pct: 0.0123, asof: "" }, KOSPI: { price: "2650.12", change_pct: -0.0085, asof: "" } },
    };
    expect(indexChips(r)).toEqual([
      { name: "KOSPI", value: "2,650.12", pct: "-0.85%", dir: "dn" },
      { name: "KOSDAQ", value: "870.50", pct: "+1.23%", dir: "up" },
    ]);
    expect(indexChips({ ...r, session: "closed" })).toEqual([]);
  });
  it("호가: 매도 높은 가격부터, 0 가격 제외, 막대 비율", () => {
    const rows = bookRows({
      asks: [["101", "10"], ["102", "20"], ["0", "0"]],
      bids: [["100", "40"], ["99", "5"]],
      total_ask: "30",
      total_bid: "45",
      time: "",
      asof: "",
    });
    expect(rows.map((r) => `${r.side}${r.price}`)).toEqual(["ask102", "ask101", "bid100", "bid99"]);
    expect(rows.find((r) => r.price === "100")?.barPct).toBe(100);
    expect(rows.find((r) => r.price === "102")?.barPct).toBe(50);
  });
  it("폴링 주기", () => {
    expect(marketPollMs("open", true, 1000)).toBe(1000);
    expect(marketPollMs("open", false, 1000)).toBe(5000);
    expect(marketPollMs("closed", true, 1000)).toBe(60000);
  });
});
