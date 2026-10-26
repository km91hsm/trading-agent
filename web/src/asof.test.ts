import { describe, expect, it } from "vitest";
import { asOfInfo, DAY_MS } from "./asof";

describe("기준 시각·오래됨", () => {
  const now = new Date("2026-10-27T03:00:00Z"); // 12:00 KST
  it("날짜만이면 그날 끝까지 신선, 넘기면 오래됨", () => {
    expect(asOfInfo("2026-10-27", now, DAY_MS)).toEqual({ text: "기준 2026-10-27", stale: false });
    expect(asOfInfo("2026-10-24", now, DAY_MS)?.stale).toBe(true);
  });
  it("ISO 시각·라벨·빈 값", () => {
    expect(asOfInfo("2026-10-27T02:30:00+00:00", now, 60 * 60 * 1000, "스냅샷")).toEqual({
      text: "스냅샷 2026-10-27 02:30",
      stale: false,
    });
    expect(asOfInfo(null, now, DAY_MS)).toBeNull();
    expect(asOfInfo("nope", now, DAY_MS)).toBeNull();
  });
});
