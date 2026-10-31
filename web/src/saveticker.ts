// 세이브티커 수집기 상태(설정 화면 칸) → 배지·작업별 줄. 순수 함수(vitest).

export type SavetickerLane = { state: string | null; error_type: string | null; stage: string | null; updated_at: string | null };
export type SavetickerStatus = {
  configured: boolean;
  backend?: "extension" | "playwright";
  service: { state: string | null; reason: string | null; at: string | null; exit_code: number | null };
  blocked: boolean;
  reason_text: string | null;
  lanes: Record<string, SavetickerLane>;
  lane_blocks: Record<string, { reason: string; at: string }>;
  last_success_at: string | null;
  health: string | null;
};

const LANE: Record<string, string> = { news: "뉴스", community: "커뮤니티", market: "시장", background: "캘린더·리포트" };
const STATE: Record<string, string> = { connecting: "Chrome 연결 중", auth_required: "로그인 필요", waiting_verification: "사용자 확인 필요", collecting: "수집 중", logging_in: "로그인 중", stopped: "멈춤", error: "오류", paused: "쉼", supervising: "감독 중" };
const REASON: Record<string, string> = { AccessDenied: "접근 거부", LoginRequired: "로그인 필요", VerificationRequired: "사이트 재확인 필요" };

/** 수집기 배지. */
export function savetickerBadge(s: SavetickerStatus): { label: string; kind: "ok" | "neutral" | "pending" | "danger" } {
  if (!s.configured) return { label: "계정 파일 없음", kind: "neutral" };
  if (s.blocked) return { label: "멈춤", kind: "danger" };
  if (Object.values(s.lanes).some(row => ["waiting_verification", "auth_required"].includes(row.state ?? ""))) return { label: "사용자 확인 필요", kind: "pending" };
  if (s.backend === "extension" && (s.service.state === "error" || Object.values(s.lanes).some(row => row.state === "error"))) return { label: "수집 오류", kind: "danger" };
  if (s.backend === "extension" && [null, "paused", "stopped", "waiting_chrome"].includes(s.service.state)) return { label: "Chrome 연결 대기", kind: "pending" };
  if (Object.keys(s.lane_blocks).length) return { label: "일부 작업 쉼", kind: "pending" };
  return { label: "수집 중", kind: "ok" };
}

/** 작업별 줄. 쉬는 작업은 이유와 다시 시도 시점을 붙인다. */
export function laneRows(s: SavetickerStatus): { lane: string; label: string; state: string; note: string }[] {
  return Object.entries(s.lanes).map(([lane, row]) => {
    const block = s.lane_blocks[lane];
    const state = block ? "쉼" : row.state ? (STATE[row.state] ?? row.state) : "—";
    const err = row.error_type ? (REASON[row.error_type] ?? row.error_type) : "";
    const note = ["waiting_verification", "auth_required"].includes(row.state ?? "") ? "전용 Chrome 탭에서 확인·로그인 후 확장 프로그램에서 재개하세요" : block ? `${REASON[block.reason] ?? block.reason} — 6시간 뒤 다시 시도` : err;
    return { lane, label: LANE[lane] ?? lane, state, note };
  });
}
