// Claude 사용량 표시용 순수 함수: 관측 경과 문구, 막대 비율, 리셋까지 남은 시간.

export type LimitWindow = { used_pct: number | null; resets_at: string | null; observed_at?: string | null } | null;
export type RateLimits = {
  observed_at: string | null;
  age_s: number | null;
  five_hour: LimitWindow;
  seven_day: LimitWindow;
  spend_limit: LimitWindow;
  source?: "session" | "probe" | "statusline"; // 세션 rate_limit_event·데몬 사용량 확인(원장) 또는 대화형 상태줄 파일
} | null;
export type Throttle = { enabled: boolean; source?: "control" | "yaml"; soft_pct: number; hard_pct: number; state: "ok" | "soft" | "hard"; reason: string | null };
export type UsageWindow = { sessions: number; cost_usd: number; avg_duration_s: number | null; errors: number };
export type UsageResp = {
  rate_limits: RateLimits;
  throttle?: Throttle;
  usage_file_present: boolean;
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

/** 관측 시각부터 지금까지 초(클라이언트 시계로 계속 흐르게). 모르면 null. */
export function ageSince(observedAt: string | null | undefined, now: Date): number | null {
  if (!observedAt) return null;
  const t = new Date(observedAt).getTime();
  return Number.isFinite(t) ? Math.max(0, Math.round((now.getTime() - t) / 1000)) : null;
}

const KST_DAY = (d: Date) =>
  new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Seoul", month: "2-digit", day: "2-digit" }).format(d).replace("-", ".");
const KST_HM = (d: Date, withDate: boolean) => {
  const hm = new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Seoul", hour: "2-digit", minute: "2-digit", hour12: false }).format(d);
  return withDate ? `${KST_DAY(d)} ${hm}` : hm;
};

/** 초기화 시각 한 줄: "21:00 초기화 · 2시간 3분 남음"(오늘이 아니면 날짜 포함). 지났으면 "초기화됨 —
 * 다음 확인 때 새 창", 모르면 null. 리셋 시각이 지나도 줄이 사라지지 않게 한다. */
export function resetLine(resetsAt: string | null | undefined, now: Date): string | null {
  const p = resetParts(resetsAt, now);
  return p ? `${p.when} · ${p.left}` : null;
}

/** 초기화 시각 두 줄(좁은 칸에서 어색하게 줄바꿈되지 않게): {when: "21:00 초기화", left: "2시간 3분 남음"}. */
export function resetParts(resetsAt: string | null | undefined, now: Date): { when: string; left: string; passed: boolean } | null {
  if (!resetsAt) return null;
  const at = new Date(resetsAt);
  if (!Number.isFinite(at.getTime())) return null;
  const ms = at.getTime() - now.getTime();
  const sameDay = KST_DAY(at) === KST_DAY(now);
  const when = KST_HM(at, !sameDay);
  if (ms <= 0) return { when: `${when} 초기화됨`, left: "다음 확인 때 새 창", passed: true };
  const m = Math.ceil(ms / 60000);
  const left = m < 60 ? `${m}분` : m < 48 * 60 ? `${Math.floor(m / 60)}시간 ${m % 60}분` : `${Math.round(m / 1440)}일`;
  return { when: `${when} 초기화`, left: `${left} 남음`, passed: false };
}

/** 관측이 6시간 넘게 오래되면 5시간 창은 이미 굴러갔을 수 있다. */
export function observationStale(ageS: number | null | undefined): boolean {
  return ageS !== null && ageS !== undefined && ageS > 6 * 3600;
}

/** 관측 출처 문구. */
export function sourceText(src: string | undefined): string {
  return src === "session"
    ? "분석 세션이 받은 값"
    : src === "probe"
      ? "데몬 사용량 확인"
      : src === "statusline"
        ? "대화형 claude 상태줄"
        : "관측";
}

/** 감속 상태 한 줄: 정상이면 "", 아니면 무엇을 막는지. */
export function throttleText(t: Throttle | undefined): string {
  if (!t || !t.enabled || t.state === "ok") return "";
  return t.state === "hard"
    ? `감속: 새 세션 전부 중지 (${Math.round(t.hard_pct * 100)}% 이상)`
    : `감속: 보유 재평가만 (${Math.round(t.soft_pct * 100)}% 이상)`;
}

/** 설정 화면 감속 상태 문구: 켜짐/꺼짐 + 기준 + 출처(화면에서 바꿨는지, 설정 파일 값인지). */
export function throttleSetting(t: Throttle | undefined): string {
  if (!t) return "상태 모름";
  const src = t.source === "control" ? "설정 화면에서 바꿈" : "설정 파일 값";
  return t.enabled
    ? `켜짐 · ${Math.round(t.soft_pct * 100)}% 부터 보유 재평가만, ${Math.round(t.hard_pct * 100)}% 부터 새 세션 중지 · ${src}`
    : `꺼짐 · 사용률과 무관하게 세션을 띄움(하루 세션 상한은 그대로) · ${src}`;
}

/** 상태 줄 칩 "5h 12% · 7d 54%". 관측이 없는 창은 0% 가 아니라 "—"(모르는 것을 0 으로 보이지 않게). */
export function usageChip(rl: NonNullable<RateLimits>): string {
  const p = (w: LimitWindow) => (w?.used_pct === null || w?.used_pct === undefined ? "—" : `${Math.round(w.used_pct)}%`);
  return `5h ${p(rl.five_hour)} · 7d ${p(rl.seven_day)}`;
}
