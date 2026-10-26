import { describe, expect, it } from "vitest";
import { lotRows, lotsHeadline, type TaxLotsResp } from "./lots";

const r: TaxLotsResp = {
  account_id: "paper-main",
  market: "US",
  year: null,
  realized: [{ ticker: "AAPL", qty: 2, buy_date: "2025-01-02", sell_date: "2025-01-12", cost: "200", proceeds: "180", pnl: "-20", term: "short", wash_sale: true }],
  open_lots: [],
  summary: { realized_pnl: "-20", short_pnl: "-20", long_pnl: "0", wash_sales: 1, unmatched_sell_qty: 0 },
  note: "",
};

describe("lots", () => {
  it("행과 머리 줄", () => {
    expect(lotRows(r)[0]).toMatchObject({ ticker: "AAPL", term: "단기", wash: true, up: false, pnl: "-$20.00" });
    expect(lotsHeadline(r)).toBe("실현 -$20.00 · 단기 -$20.00 · 장기 $0.00 · 워시세일 1건");
    expect(lotsHeadline({ ...r, market: "KR", summary: { ...r.summary, wash_sales: 0 } })).toBe("실현 -20원");
    expect(lotRows(null)).toEqual([]);
  });
});
