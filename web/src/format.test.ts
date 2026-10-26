import { describe, expect, it } from "vitest";
import { fmtDate, fmtDelta, fmtMoney, fmtPct, fmtTime, marketDate } from "./format";

describe("fmtMoney", () => {
  it("KR 은 정수 천단위 + 원", () => {
    expect(fmtMoney("2580000", "KR")).toBe("2,580,000원");
    expect(fmtMoney(2580000.4, "KR")).toBe("2,580,000원");
  });
  it("US 는 $ 소수 2자리", () => {
    expect(fmtMoney("231.8", "US")).toBe("$231.80");
  });
  it("null·빈 값은 대시", () => {
    expect(fmtMoney(null, "KR")).toBe("—");
    expect(fmtMoney("", "US")).toBe("—");
  });
});

describe("fmtPct", () => {
  it("소수 2자리 + %", () => {
    expect(fmtPct(0.0153)).toBe("1.53%");
    expect(fmtPct(-0.019, 1)).toBe("-1.9%");
    expect(fmtPct(null)).toBe("—");
  });
});

describe("fmtDelta", () => {
  it("절대·퍼센트 변화를 소수 2자리로", () => {
    expect(fmtDelta(232500, 229000)).toEqual({ abs: 3500, pct: 1.53 });
    expect(fmtDelta("231.8", "233.2")).toEqual({ abs: -1.4, pct: -0.6 });
  });
  it("기준이 없거나 0 이면 null", () => {
    expect(fmtDelta(100, null)).toBeNull();
    expect(fmtDelta(100, 0)).toBeNull();
  });
});

describe("fmtTime / fmtDate", () => {
  it("KR 은 KST, US 는 ET 의 HH:MM", () => {
    expect(fmtTime("2026-10-23T00:31:00+00:00", "KR")).toBe("09:31");
    expect(fmtTime("2026-10-23T00:31:00+00:00", "US")).toBe("20:31");
    expect(fmtTime(null, "KR")).toBe("—");
  });
  it("날짜는 YYYY-MM-DD (KST)", () => {
    expect(fmtDate("2026-10-22T16:30:00+00:00")).toBe("2026-10-23");
    expect(fmtDate("2026-10-23")).toBe("2026-10-23");
  });
});

describe("marketDate (전체 검토 W11)", () => {
  it("US 주문은 뉴욕 날짜", () => {
    expect(marketDate("2026-10-26T19:00:00Z", "US")).toBe("2026-10-26");
    expect(marketDate("2026-10-26T19:00:00Z", "KR")).toBe("2026-10-27");
  });
});
