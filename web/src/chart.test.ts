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
