import { describe, expect, it } from "vitest";
import { areaLine, opsRows, opsStale, type OpsResp } from "./ops";

const R: OpsResp = {
  level: "warning",
  label: "주의",
  at: "2026-10-24T01:00:00+00:00",
  areas: { loss: "ok", orders: "warning", reconcile: "ok", jobs: "watch", llm: "ok" },
  items: [
    { key: "jobs.missed", level: "watch", text: "놓친 잡 1건", link: "/system", count: 1 },
    { key: "orders.unknown", level: "warning", text: "unknown 2건", link: "/orders?status=unknown", count: 2 },
  ],
  actions: [],
  headline: "점검 주의 · 2건 · 10:00",
  source: "daemon",
};

describe("ops (운영 점검)", () => {
  it("행은 심각한 것 먼저, 영역 라벨·위험 표시", () => {
    const rows = opsRows(R);
    expect(rows.map((r) => r.key)).toEqual(["orders.unknown", "jobs.missed"]);
    expect(rows[0]).toMatchObject({ area: "주문", levelLabel: "주의", danger: true });
    expect(rows[1].danger).toBe(false);
    expect(opsRows(null)).toEqual([]);
  });
  it("영역 한 줄", () => {
    expect(areaLine(R)).toBe("손익 정상 · 주문 주의 · 대조 정상 · 데몬·잡 관심 · 세션 정상");
  });
});

describe("opsStale (전체 검토 W7)", () => {
  const base = { level: "ok" as const, label: "정상", areas: {}, items: [], actions: [], headline: "", source: "daemon" as const };
  it("데몬 결과가 45분 넘게 묵으면 오래됨, 지금 판정은 아님", () => {
    const now = new Date("2026-10-26T05:00:00Z");
    expect(opsStale({ ...base, at: "2026-10-26T04:30:00Z" }, now)).toBe(false);
    expect(opsStale({ ...base, at: "2026-10-26T04:00:00Z" }, now)).toBe(true);
    expect(opsStale({ ...base, at: "2026-10-26T01:00:00Z", source: "live" }, now)).toBe(false);
  });
});
