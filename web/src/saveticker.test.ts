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

it("일반 Chrome 연결 해제와 사용자 확인 대기를 수집 중으로 표시하지 않는다", () => {
  const s = { ...base, backend: "extension" as const, lane_blocks: {}, service: { ...base.service, state: "paused", reason: "extension_disconnected" } };
  expect(savetickerBadge(s)).toEqual({ label: "Chrome 연결 대기", kind: "pending" });
  expect(savetickerBadge({ ...s, service: { ...s.service, state: "collecting", reason: null }, lanes: { news: { state: "waiting_verification", error_type: null, stage: null, updated_at: null } } })).toEqual({ label: "사용자 확인 필요", kind: "pending" });
  expect(savetickerBadge({ ...s, service: { ...s.service, state: "error" } })).toEqual({ label: "수집 오류", kind: "danger" });
  expect(savetickerBadge({ ...s, service: { ...s.service, state: "collecting" }, lanes: { news: { state: "error", error_type: null, stage: null, updated_at: null } } })).toEqual({ label: "수집 오류", kind: "danger" });
});
