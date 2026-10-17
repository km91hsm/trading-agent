import { describe, expect, it } from "vitest";
import { markerStyle } from "./pages/Chart";

describe("markerStyle", () => {
  it("주문은 방향 화살표, 결정은 원, 트리거는 사각형", () => {
    expect(markerStyle({ kind: "order", side: "BUY" })).toEqual({ position: "belowBar", color: "#f5a524", shape: "arrowUp" });
    expect(markerStyle({ kind: "order", side: "SELL" })).toEqual({ position: "aboveBar", color: "#f5a524", shape: "arrowDown" });
    expect(markerStyle({ kind: "decision", side: "HOLD" })).toEqual({ position: "belowBar", color: "#8b5cf6", shape: "circle" });
    expect(markerStyle({ kind: "trigger", side: "TRIGGER" })).toEqual({ position: "aboveBar", color: "#2dd4bf", shape: "square" });
  });
});

describe("formatSessionTime", () => {
  it("UTC epoch 초를 세션 타임존의 HH:MM 으로", async () => {
    const { formatSessionTime } = await import("./pages/Chart");
    expect(formatSessionTime(Date.UTC(2026, 9, 9, 0, 0) / 1000, "Asia/Seoul")).toBe("09:00");
    expect(formatSessionTime(Date.UTC(2026, 9, 9, 13, 30) / 1000, "America/New_York")).toBe("09:30");
    expect(formatSessionTime("2026-10-09", "Asia/Seoul")).toBe("2026-10-09"); // 일봉 문자열은 그대로
  });
});
