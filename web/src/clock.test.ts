import { describe, expect, it } from "vitest";
import { clockText } from "./clock";

describe("clockText", () => {
  it("KST 시각·날짜·뉴욕 시각·장 상태", () => {
    const t = clockText(new Date("2026-10-26T12:06:05Z")); // 21:06:05 KST 월 · 08:06 ET
    expect(t.kst).toBe("21:06:05");
    expect(t.kstShort).toBe("21:06");
    expect(t.et).toBe("08:06");
    expect(t.date).toBe("10.26 (월)");
    expect(t.phase).toContain("KR");
  });
});
