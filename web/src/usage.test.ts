import { describe, expect, it } from "vitest";
import { agoText, barPct, observationStale, resetsIn, usageLevel } from "./usage";

describe("usage", () => {
  it("agoText", () => {
    expect(agoText(null)).toBe("관측 없음");
    expect(agoText(10)).toBe("방금");
    expect(agoText(180)).toBe("3분 전");
    expect(agoText(7200)).toBe("2시간 전");
    expect(agoText(3 * 86400)).toBe("3일 전");
  });
  it("barPct 는 0~100 으로 자른다", () => {
    expect(barPct(null)).toBe(0);
    expect(barPct(12.4)).toBe(12.4);
    expect(barPct(140)).toBe(100);
    expect(barPct(-3)).toBe(0);
  });
  it("usageLevel 단계", () => {
    expect(usageLevel(null)).toBe("none");
    expect(usageLevel(10)).toBe("ok");
    expect(usageLevel(75)).toBe("warn");
    expect(usageLevel(95)).toBe("danger");
  });
  it("resetsIn", () => {
    const now = new Date("2026-10-23T03:00:00Z");
    expect(resetsIn(null, now)).toBeNull();
    expect(resetsIn("2026-10-23T02:00:00Z", now)).toBeNull();
    expect(resetsIn("2026-10-23T03:30:00Z", now)).toBe("30분 뒤 리셋");
    expect(resetsIn("2026-10-23T05:10:00Z", now)).toBe("2시간 10분 뒤 리셋");
    expect(resetsIn("2026-10-27T03:00:00Z", now)).toBe("4일 뒤 리셋");
  });
  it("관측이 6시간 넘으면 stale", () => {
    expect(observationStale(3600)).toBe(false);
    expect(observationStale(7 * 3600)).toBe(true);
    expect(observationStale(null)).toBe(false);
  });
});
