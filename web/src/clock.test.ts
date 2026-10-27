import { describe, expect, it } from "vitest";
import { clockText } from "./clock";

describe("clockText", () => {
  it("KST 시각·날짜·뉴욕 시각·장 상태", () => {
    const t = clockText(new Date("2026-10-26T12:06:05Z")); // 21:06:05 KST 월 · 08:06 ET
    expect(t.kst).toBe("21:06:05");
    expect(t.kstShort).toBe("21:06");
    expect(t.et).toBe("08:06:05");
    expect(t.etShort).toBe("08:06");
    expect(t.etDate).toBe("10.26 (월)");
    expect(t.date).toBe("10.26 (월)");
    expect(t.phase).toContain("KR");
  });

  it("뉴욕 날짜는 뉴욕 기준(한국 새벽이면 하루 전)", () => {
    const t = clockText(new Date("2026-10-26T16:30:00Z")); // 10.27(화) 01:30 KST · 10.26(월) 12:30 ET
    expect(t.date).toBe("10.27 (화)");
    expect(t.etDate).toBe("10.26 (월)");
    expect(t.et).toBe("12:30:00");
  });
});
