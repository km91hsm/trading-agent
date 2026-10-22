import { describe, expect, it } from "vitest";
import { marketPhase } from "./tape";

describe("marketPhase", () => {
  it("KR 장중(13:41 KST) · US 개장 전(00:41 ET)", () => {
    const p = marketPhase(new Date("2026-10-23T13:41:00+09:00"));
    expect(p.kr).toBe("open");
    expect(p.us).toBe("pre");
    expect(p.label).toBe("KR 정규장 · US 개장 전");
  });
  it("KR 마감(22:41 KST) · US 장중(09:41 ET)", () => {
    const p = marketPhase(new Date("2026-10-23T22:41:00+09:00"));
    expect(p.kr).toBe("closed");
    expect(p.us).toBe("open");
    expect(p.label).toBe("KR 마감 · US 정규장");
  });
  it("주말은 둘 다 휴장", () => {
    const p = marketPhase(new Date("2026-10-24T13:00:00+09:00"));
    expect(p.kr).toBe("weekend");
    expect(p.us).toBe("weekend");
    expect(p.label).toBe("KR 휴장 · US 휴장");
  });
});
