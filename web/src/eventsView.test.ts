import { describe, expect, it } from "vitest";
import { kindLabel, rolesLine, todayEvents, type EventsResp } from "./eventsView";

describe("사건 수혜", () => {
  it("역할별로 묶는다", () => {
    expect(
      rolesLine([
        { ticker: "012450", name: "한화에어로스페이스", role: "방산" },
        { ticker: "064350", name: "현대로템", role: "방산" },
        { ticker: "132030", name: "KODEX 골드선물(H)", role: "금" },
      ]),
    ).toBe("방산: 한화에어로스페이스, 현대로템 · 금: KODEX 골드선물(H)");
  });
  it("오늘 것만", () => {
    const r: EventsResp = { enabled: true, market: "KR", date: "2026-10-27", events: [{ id: "x", label: "x", kind: "shock", hits: 3, news: [], tickers: [] }], table: [] };
    expect(todayEvents(r, "2026-10-27")).toHaveLength(1);
    expect(todayEvents(r, "2026-10-28")).toHaveLength(0);
    expect(kindLabel("structural")).toBe("구조적 흐름");
  });
});
