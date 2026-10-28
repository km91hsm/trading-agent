import { describe, expect, it } from "vitest";
import { laneRows, savetickerBadge, type SavetickerStatus } from "./saveticker";

const base: SavetickerStatus = {
  configured: true,
  service: { state: "running", reason: null, at: null, exit_code: null },
  blocked: false,
  reason_text: null,
  lanes: {
    news: { state: "collecting", error_type: null, stage: null, updated_at: "2026-10-29T01:00:00+00:00" },
    community: { state: "paused", error_type: null, stage: null, updated_at: null },
  },
  lane_blocks: { community: { reason: "AccessDenied", at: "2026-10-29T01:00:00+00:00" } },
  last_success_at: "2026-10-29T01:00:00+00:00",
  health: "ok",
};

describe("세이브티커 상태(설정 화면)", () => {
  it("배지: 계정 없음·멈춤·일부 쉼·수집 중", () => {
    expect(savetickerBadge({ ...base, configured: false })).toEqual({ label: "계정 파일 없음", kind: "neutral" });
    expect(savetickerBadge({ ...base, blocked: true })).toEqual({ label: "멈춤", kind: "danger" });
    expect(savetickerBadge(base)).toEqual({ label: "일부 작업 쉼", kind: "pending" });
    expect(savetickerBadge({ ...base, lane_blocks: {} })).toEqual({ label: "수집 중", kind: "ok" });
  });
  it("작업별 줄: 이름·상태·쉬는 이유", () => {
    expect(laneRows(base)).toEqual([
      { lane: "news", label: "뉴스", state: "수집 중", note: "" },
      { lane: "community", label: "커뮤니티", state: "쉼", note: "접근 거부 — 6시간 뒤 다시 시도" },
    ]);
  });
});
