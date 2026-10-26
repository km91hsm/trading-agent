import { describe, expect, it } from "vitest";
import { applyQuotes, CLOSED_RECHECK_MS, LIVE_INTERVAL_MS, liveLabel, nextDelay } from "./live";
import type { Position } from "./types";

const pos = (ticker: string, qty: number, avg: string, cur: string): Position => ({
  ticker,
  market: "KR",
  qty,
  avg_price: avg,
  current_price: cur,
  eval_amount: String(qty * Number(cur)),
});

describe("applyQuotes", () => {
  it("실시간 값으로 평가·NAV·손익을 다시 계산, stale 는 무시", () => {
    const r = applyQuotes([pos("005930", 2, "100", "100"), pos("000660", 1, "50", "60")], {
      "005930": { price: "110", change_pct: 0.01, asof: "x", stale: false },
      "000660": { price: "999", change_pct: null, asof: "x", stale: true },
    }, "1000");
    expect(r.positions[0]).toMatchObject({ current_price: "110", eval_amount: "220", live: true });
    expect(r.positions[1]).toMatchObject({ current_price: "60", live: false });
    expect(r.nav).toBe(1000 + 220 + 60);
    expect(r.pnl).toBe(220 + 60 - 250);
    expect(r.liveCount).toBe(1);
  });
  it("시세가 없으면 스냅샷 그대로", () => {
    const r = applyQuotes([pos("005930", 1, "100", "100")], null, null);
    expect(r.nav).toBeNull();
    expect(r.positions[0].live).toBe(false);
  });
});

describe("폴링 주기·표기", () => {
  it("장중 10초, 장 밖 5분", () => {
    expect(nextDelay({ session: "open", asof: "", quotes: {} })).toBe(LIVE_INTERVAL_MS);
    expect(nextDelay({ session: "closed", asof: "", quotes: {} })).toBe(CLOSED_RECHECK_MS);
    expect(nextDelay(null)).toBe(CLOSED_RECHECK_MS);
    expect(liveLabel({ session: "closed", asof: "", quotes: {} }, 0)).toBe("장 마감 · 스냅샷 값");
    expect(liveLabel({ session: "open", asof: "", quotes: {} }, 2)).toBe("실시간 2종목 · 10초마다");
  });
});
