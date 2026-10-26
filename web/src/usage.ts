// Claude 사용량 표시용 순수 함수: 관측 경과 문구, 막대 비율, 리셋까지 남은 시간.

export type LimitWindow = { used_pct: number | null; resets_at: string | null } | null;
export type RateLimits = {
  observed_at: string | null;
  age_s: number | null;
  five_hour: LimitWindow;
  seven_day: LimitWindow;
  spend_limit: LimitWindow;
  source?: "session" | "statusline"; // Phase 24: 세션 rate_limit_event 원장 또는 대화형 상태줄 파일
} | null;
export type Throttle = { enabled: boolean; soft_pct: number; hard_pct: number; state: "ok" | "soft" | "hard"; reason: string | null };
export type UsageWindow = { sessions: number; cost_usd: number; avg_duration_s: number | null; errors: number };
export type UsageResp = {
  rate_limits: RateLimits;
  throttle?: Throttle;
  usage_file: string | null;
  windows: { h5: UsageWindow; d7: UsageWindow; today: { sessions: number; cap: number } };
  note: string;
};

/** "방금" / "3분 전" / "2시간 전" / "3일 전". null 은 "관측 없음". */
export function agoText(ageS: number | null | undefined): string {
  if (ageS === null || ageS === undefined) return "관측 없음";
  if (ageS < 60) return "방금";
  const m = Math.round(ageS / 60);
  if (m < 60) return `${m}분 전`;
  const h = Math.round(m / 60);
  if (h < 48) return `${h}시간 전`;
  return `${Math.round(h / 24)}일 전`;
}

/** 막대 폭(0~100 으로 자름). null 은 0. */
export function barPct(usedPct: number | null | undefined): number {
  if (usedPct === null || usedPct === undefined || !Number.isFinite(usedPct)) return 0;
  return Math.max(0, Math.min(100, usedPct));
}

/** 사용률 단계: 70% 미만 ok, 90% 미만 warn, 그 밖엔 danger. */
export function usageLevel(usedPct: number | null | undefined): "ok" | "warn" | "danger" | "none" {
  if (usedPct === null || usedPct === undefined) return "none";
  if (usedPct < 70) return "ok";
  if (usedPct < 90) return "warn";
  return "danger";
}

/** 리셋까지 남은 시간 문구. 지났거나 모르면 null. */
export function resetsIn(resetsAt: string | null | undefined, now: Date): string | null {
  if (!resetsAt) return null;
  const ms = new Date(resetsAt).getTime() - now.getTime();
  if (!Number.isFinite(ms) || ms <= 0) return null;
  const m = Math.round(ms / 60000);
  if (m < 60) return `${m}분 뒤 리셋`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}시간 ${m % 60}분 뒤 리셋`;
  return `${Math.round(h / 24)}일 뒤 리셋`;
}

/** 관측이 6시간 넘게 오래되면 5시간 창은 이미 굴러갔을 수 있다. */
export function observationStale(ageS: number | null | undefined): boolean {
  return ageS !== null && ageS !== undefined && ageS > 6 * 3600;
}

/** 관측 출처 문구. */
export function sourceText(src: string | undefined): string {
  return src === "session" ? "분석 세션이 받은 값" : src === "statusline" ? "대화형 claude 상태줄" : "관측";
}

/** 감속 상태 한 줄: 정상이면 "", 아니면 무엇을 막는지. */
export function throttleText(t: Throttle | undefined): string {
  if (!t || !t.enabled || t.state === "ok") return "";
  return t.state === "hard"
    ? `감속: 새 세션 전부 중지 (${Math.round(t.hard_pct * 100)}% 이상)`
    : `감속: 보유 재평가만 (${Math.round(t.soft_pct * 100)}% 이상)`;
}
