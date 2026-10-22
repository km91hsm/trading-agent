import { describe, expect, it } from "vitest";
import { DEFAULT_PALETTE, chartPalette, formatSessionTime, markerSize, markerStyle } from "./chart";

describe("markerStyle", () => {
  it("주문은 방향 화살표(먹색), 결정은 원(청색), 트리거는 사각형(황색)", () => {
    const p = DEFAULT_PALETTE;
    expect(markerStyle({ kind: "order", side: "BUY" })).toEqual({ position: "belowBar", color: p.ink, shape: "arrowUp" });
    expect(markerStyle({ kind: "order", side: "SELL" })).toEqual({ position: "aboveBar", color: p.ink, shape: "arrowDown" });
    expect(markerStyle({ kind: "decision", side: "HOLD" })).toEqual({ position: "belowBar", color: p.dn, shape: "circle" });
    expect(markerStyle({ kind: "trigger", side: "TRIGGER" })).toEqual({ position: "aboveBar", color: p.pending, shape: "square" });
  });
  it("팔레트를 넘기면 그 색을 쓴다", () => {
    const custom = { ...DEFAULT_PALETTE, ink: "rgb(1, 2, 3)" };
    expect(markerStyle({ kind: "order", side: "BUY" }, custom).color).toBe("rgb(1, 2, 3)");
  });
});

describe("markerSize", () => {
  it("확신 3단계", () => {
    expect(markerSize(0.3)).toBe(0);
    expect(markerSize(0.55)).toBe(1);
    expect(markerSize(0.72)).toBe(2);
    expect(markerSize(null)).toBe(1);
  });
});

describe("chartPalette", () => {
  it("요소가 없으면 기본 팔레트", () => {
    expect(chartPalette(null)).toEqual(DEFAULT_PALETTE);
  });
});

describe("formatSessionTime", () => {
  it("UTC epoch 초를 세션 타임존의 HH:MM 으로", () => {
    expect(formatSessionTime(Date.UTC(2026, 9, 9, 0, 0) / 1000, "Asia/Seoul")).toBe("09:00");
    expect(formatSessionTime(Date.UTC(2026, 9, 9, 13, 30) / 1000, "America/New_York")).toBe("09:30");
    expect(formatSessionTime("2026-10-09", "Asia/Seoul")).toBe("2026-10-09"); // 일봉 문자열은 그대로
  });
});
