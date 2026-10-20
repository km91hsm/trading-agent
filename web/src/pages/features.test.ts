import { describe, expect, it } from "vitest";
import { fmt, sortRows } from "./features";

const rows = [
  { ticker: "A", roe: 0.1 },
  { ticker: "B", roe: null },
  { ticker: "C", roe: 0.3 },
];

describe("sortRows", () => {
  it("내림차순 정렬에서도 null 은 항상 마지막", () => {
    expect(sortRows(rows, "roe", "desc").map((r) => r.ticker)).toEqual(["C", "A", "B"]);
    expect(sortRows(rows, "roe", "asc").map((r) => r.ticker)).toEqual(["A", "C", "B"]);
  });
  it("문자열 열도 정렬하고 원본은 바꾸지 않는다", () => {
    expect(sortRows(rows, "ticker", "desc").map((r) => r.ticker)).toEqual(["C", "B", "A"]);
    expect(rows.map((r) => r.ticker)).toEqual(["A", "B", "C"]);
  });
});

describe("fmt", () => {
  it("숫자는 소수 4자리, null 은 -, 문자열 그대로", () => {
    expect(fmt(0.123456)).toBe("0.1235");
    expect(fmt(null)).toBe("-");
    expect(fmt("KR")).toBe("KR");
    expect(fmt(71500)).toBe("71500");
  });
});
