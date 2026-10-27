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
  job_queue: "요청 작업 실행기",
  features_backfill: "피처 백필",
  features_snapshot: "피처 스냅샷",
  features_derive: "파생 열 재계산",
  rl_train: "RL 학습",
  analyze_ticker: "종목 분석",
  market_view: "시장 세션",
  reevaluate: "재평가",
  news_collect: "뉴스 수집",
  reconcile: "KIS 대조",
  discover: "장중 발굴",
  scalp: "분 단위 단타",
  theme: "테마 열기",
  ops_check: "운영 점검",
  job_queue_fast: "빠른 작업 실행기",
  job_queue_reeval: "재평가 실행기",
  job_queue_info: "정보 확인 실행기",
  rebalance: "주간 리밸런스",
  balance_refresh: "잔고 갱신",
  usage_probe: "Claude 사용량 확인",
  daily_report: "일일 리포트",
  ledger_backup: "원장 백업",
  universe_members: "유니버스 구성 기록",
  ksd_events: "권리 일정 수집",
  morning_briefing: "아침 브리핑",
  paper_review: "모의 체결 점검",
  saveticker: "세이브티커 수집",
};

export function jobLabel(j: Pick<JobRow, "id" | "kind" | "market">): string {
  const base = KIND_LABEL[j.kind] ?? j.id;
  return j.market ? `${j.market} ${base}` : base;
}

const DOW: Record<string, string> = {
  "mon-fri": "평일",
  "mon-sat": "월–토",
  "mon-sun": "매일",
  mon: "월",
  tue: "화",
  wed: "수",
  thu: "목",
  fri: "금",
  sat: "토",
  sun: "일",
};

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

// --- 작업 대기열(요청) -------------------------------------------------------------------

export type RequestStatus = "queued" | "running" | "ok" | "error" | "skipped";
export type JobRequest = {
  id: number;
  kind: string;
  params: Record<string, unknown>;
  status: RequestStatus;
  source: string;
  requested_at: string;
  started_at: string | null;
  finished_at: string | null;
  progress: string | null;
  error: string | null;
  label?: string;
  priority?: number;
};
export type JobsResp = { alive: boolean; active: number; requests: JobRequest[] };

export const JOBS_POLL_MS = 3000;
export const JOBS_CHANNEL = "trader-jobs";

/** 활성 요청이 있을 때 이 탭이 /api/jobs 를 폴링할까: SSE 리더 탭(가려져 있어도 — 중계 몫) 또는
 * **보이는** 탭. 리더 탭에 작업 표가 없으면(대시보드 리더 + 시스템 현황 팔로워) 보이는 탭이 직접
 * 폴링해야 진행 문구가 멈추지 않는다(Codex 리뷰). 가려진 팔로워는 중계만 받는다. */
export function pollsJobs(active: boolean, leader: boolean, visible = false): boolean {
  return active && (leader || visible);
}

export const REQUEST_STATUS_LABEL: Record<RequestStatus, string> = {
  queued: "대기",
  running: "실행 중",
  ok: "완료",
  error: "오류",
  skipped: "건너뜀",
};

/** 요청 한 줄 라벨. 서버 label 이 있으면 그대로, 없으면 kind·params 로 만든다. */
export function requestLabel(r: Pick<JobRequest, "kind" | "params" | "label">): string {
  if (r.label) return r.label;
  const base = KIND_LABEL[r.kind] ?? r.kind;
  const market = typeof r.params.market === "string" ? r.params.market : null;
  if (r.kind === "analyze_ticker") return `${base} ${String(r.params.ticker ?? "")}${r.params.dry_run === false ? "" : " (모의)"}`;
  if (r.kind === "features_backfill") return `${base} ${String(r.params.years ?? 3)}년${market ? ` ${market}` : ""}`;
  return market ? `${market} ${base}` : base;
}

/** 잡 표 행 → '지금 실행' 요청. 버튼이 없는 잡(monitor·telegram_poll·job_queue)은 null. */
export function runNowRequest(j: Pick<JobRow, "id" | "kind" | "market">): { kind: string; params: Record<string, unknown> } | null {
  if (j.kind === "scan_analyze") return { kind: "scan_analyze", params: { market: j.market ?? "KR" } };
  if (j.kind === "ml_train" || j.kind === "reflect" || j.kind === "fills_sync") return { kind: j.kind, params: {} };
  return null;
}

/** 버튼을 살릴 수 있는가. 사유는 비활성 버튼의 title 과 표 아래 한 줄에 쓴다. */
export function canRun(d: Pick<DaemonStatus, "alive"> | null, token: string | null): { ok: boolean; reason?: string } {
  if (!token) return { ok: false, reason: "토큰이 필요해요 (설정/제어에서 입력)" };
  if (!d || !d.alive) return { ok: false, reason: "데몬이 꺼져 있어요 — 요청은 데몬이 실행해요" };
  return { ok: true };
}

export type RequestRowView = {
  key: number;
  id: number;
  label: string;
  kind: string;
  state: RequestStatus;
  stateLabel: string;
  when: string | null; // 대기면 요청 시각, 아니면 종료(없으면 시작) 시각
  detail: string | null; // 실행 중이면 진행 문구, 오류·건너뜀이면 사유
  cancellable: boolean;
  priority: boolean; // 우선 실행(재평가 등 priority > 0) — 대기 중일 때만 뜻이 있다
};

export function requestRows(reqs: JobRequest[]): RequestRowView[] {
  return reqs.map((r) => ({
    key: r.id,
    id: r.id,
    label: requestLabel(r),
    kind: r.kind,
    state: r.status,
    stateLabel: REQUEST_STATUS_LABEL[r.status] ?? r.status,
    when: r.status === "queued" ? r.requested_at : (r.finished_at ?? r.started_at ?? r.requested_at),
    detail: r.status === "running" ? r.progress : r.status === "error" || r.status === "skipped" ? r.error : null,
    cancellable: r.status === "queued",
    priority: (r.priority ?? 0) > 0,
  }));
}
