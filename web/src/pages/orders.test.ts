import { describe, expect, it } from "vitest";
import { approvalNeedsConfirm } from "./Orders";

describe("approvalNeedsConfirm (전체 검토 W1)", () => {
  const accounts = [
    { id: "paper-main", env: "paper" },
    { id: "real-main", env: "real" },
  ];
  it("행의 계좌가 실전이면 확인 — 지금 고른 계좌와 무관", () => {
    expect(approvalNeedsConfirm({ account_id: "real-main" }, accounts)).toBe(true);
    expect(approvalNeedsConfirm({ account_id: "paper-main" }, accounts)).toBe(false);
  });
  it("모르는 계좌도 확인(안전 쪽)", () => {
    expect(approvalNeedsConfirm({ account_id: "ghost" }, accounts)).toBe(true);
  });
});
