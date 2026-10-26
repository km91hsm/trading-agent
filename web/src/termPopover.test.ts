import { describe, expect, it } from "vitest";
import { popoverPosition } from "./termPopover";

describe("popoverPosition", () => {
  const vp = { width: 1000, height: 800 };
  it("기본은 용어 바로 아래, 왼쪽 맞춤", () => {
    expect(popoverPosition({ left: 100, top: 200, bottom: 220 }, vp)).toEqual({ position: "fixed", left: 100, top: 226, maxWidth: 280 });
  });
  it("오른쪽 끝이면 화면 안으로 당기고, 아래가 모자라면 위로", () => {
    const p = popoverPosition({ left: 950, top: 760, bottom: 780 }, vp);
    expect(p.left).toBe(1000 - 280 - 8);
    expect(p.top).toBeUndefined();
    expect(p.bottom).toBe(800 - 760 + 6);
  });
  it("좁은 화면에선 폭을 줄인다", () => {
    expect(popoverPosition({ left: 0, top: 10, bottom: 30 }, { width: 250, height: 600 }).maxWidth).toBe(234);
  });
});
