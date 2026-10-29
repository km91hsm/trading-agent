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
  it("재확인 오류는 '사이트 재확인 필요'", () => {
    const s = { ...base, lane_blocks: {}, lanes: { news: { state: "error", error_type: "VerificationRequired", stage: "login_profile", updated_at: null } } };
    expect(laneRows(s)[0]).toEqual({ lane: "news", label: "뉴스", state: "오류", note: "사이트 재확인 필요" });
  });
});

it("재확인 대기를 수집 중으로 표시하지 않고 브라우저 조작을 안내한다", () => {
  const s = { ...base, lane_blocks: {}, lanes: { news: { state: "waiting_verification", error_type: null, stage: "waiting_verification", updated_at: null } } };
  expect(savetickerBadge(s)).toEqual({ label: "사이트 재확인 대기", kind: "pending" });
  expect(laneRows(s)[0]).toEqual({ lane: "news", label: "뉴스", state: "사이트 재확인 대기", note: "열린 수집 브라우저에서 재확인·로그인을 완료하세요(최대 10분)" });
});
