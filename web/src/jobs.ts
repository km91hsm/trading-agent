// 데몬 잡 표: 잡 id → 한국어 라벨, 주기 문구, 마지막 실행 상태 분류, 데몬 생존 문구 (순수 함수).

export type JobRow = {
  id: string;
  kind: string;
  market: string | null;
  cron: Record<string, unknown> | null;
  interval_s: number | null;
  last_start: string | null;
  last_end: string | null;
  last_status: string | null;
  last_error: string | null;
  next_run: string | null;
};
export type DaemonStatus = {
  alive: boolean;
  pid: number | null;
  started_at: string | null;
  uptime_s: number | null;
  heartbeat_pid: number | null;
  pid_mismatch: boolean;
  updated_at: string | null;
  age_s: number | null;
  stale: boolean;
  stopped_at: string | null;
  stale_after_s: number;
  jobs: JobRow[];
};

const KIND_LABEL: Record<string, string> = {
  scan_analyze: "스캔 → 분석",
  monitor: "장중 감시",
  telegram_poll: "텔레그램 명령",
  fills_sync: "체결 동기화",
  reflect: "복기",
  ml_train: "퀀트 학습",
};

export function jobLabel(j: Pick<JobRow, "id" | "kind" | "market">): string {
  const base = KIND_LABEL[j.kind] ?? j.id;
  return j.market ? `${j.market} ${base}` : base;
}

const DOW: Record<string, string> = { "mon-fri": "평일", "mon-sat": "월–토", sat: "토", sun: "일", "mon-sun": "매일" };

/** "매일 07:30" / "평일 07:30 (뉴욕)" / "60초마다". */
export function scheduleText(j: Pick<JobRow, "cron" | "interval_s">): string {
  if (j.interval_s) return j.interval_s % 60 === 0 ? `${j.interval_s / 60}분마다` : `${j.interval_s}초마다`;
  const c = j.cron ?? {};
  const hh = String(c.hour ?? "*").padStart(2, "0");
  const mm = String(c.minute ?? 0).padStart(2, "0");
  const dow = typeof c.day_of_week === "string" ? (DOW[c.day_of_week] ?? c.day_of_week) : "매일";
  const tz = typeof c.timezone === "string" && c.timezone !== "Asia/Seoul" ? (c.timezone === "America/New_York" ? " (뉴욕)" : ` (${c.timezone})`) : "";
  return `${dow} ${hh}:${mm}${tz}`;
}

export type JobState = "ok" | "error" | "running" | "missed" | "skipped" | "none";

export function jobState(j: Pick<JobRow, "last_status">): JobState {
  const s = j.last_status;
  if (!s) return "none";
  if (s === "ok" || s === "error" || s === "running" || s === "missed" || s === "skipped") return s;
  return "none";
}

export const JOB_STATE_LABEL: Record<JobState, string> = {
  ok: "정상",
  error: "오류",
  running: "실행 중",
  missed: "놓침",
  skipped: "건너뜀",
  none: "기록 없음",
};

/** 상태줄 문구: 실행 중(가동 n시간) / 멈춤 / 응답 없음(마지막 틱 n분 전). */
export function daemonText(d: DaemonStatus | null): { text: string; level: "ok" | "err" | "warn" | "none" } {
  if (!d) return { text: "확인 중", level: "none" };
  if (!d.alive) {
    return { text: d.stopped_at ? "멈춤 (정상 종료)" : d.updated_at ? "멈춤" : "실행 기록 없음", level: "err" };
  }
  const up = d.uptime_s ?? 0;
  const upText = up < 3600 ? `${Math.round(up / 60)}분째` : up < 48 * 3600 ? `${Math.floor(up / 3600)}시간째` : `${Math.round(up / 86400)}일째`;
  if (d.stale) return { text: `응답 없음 · 마지막 틱 ${Math.round((d.age_s ?? 0) / 60)}분 전`, level: "warn" };
  return { text: `실행 중 · ${upText}`, level: "ok" };
}
