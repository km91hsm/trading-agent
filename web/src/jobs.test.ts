import { describe, expect, it } from "vitest";
import { canRun, daemonText, jobLabel, jobState, pollsJobs, requestLabel, requestRows, runNowRequest, scheduleText, type DaemonStatus, type JobRequest } from "./jobs";

const base: DaemonStatus = {
  alive: true, pid: 7, started_at: null, uptime_s: 2 * 3600, heartbeat_pid: 7, pid_mismatch: false,
  updated_at: null, age_s: 30, stale: false, stopped_at: null, stale_after_s: 600, jobs: [],
};

describe("jobs", () => {
  it("라벨은 시장 + 종류", () => {
    expect(jobLabel({ id: "kr_scan_analyze", kind: "scan_analyze", market: "KR" })).toBe("KR 스캔 → 분석");
    expect(jobLabel({ id: "telegram_poll", kind: "telegram_poll", market: null })).toBe("텔레그램 명령");
    expect(jobLabel({ id: "weird", kind: "weird", market: null })).toBe("weird");
  });
  it("주기 문구", () => {
    expect(scheduleText({ cron: { hour: 7, minute: 30, day_of_week: "mon-fri" }, interval_s: null })).toBe("평일 07:30");
    expect(scheduleText({ cron: { hour: 8, minute: 30, day_of_week: "mon-fri", timezone: "America/New_York" }, interval_s: null })).toBe("평일 08:30 (뉴욕)");
    expect(scheduleText({ cron: null, interval_s: 60 })).toBe("1분마다");
    expect(scheduleText({ cron: null, interval_s: 30 })).toBe("30초마다");
  });
  it("상태 분류", () => {
    expect(jobState({ last_status: null })).toBe("none");
    expect(jobState({ last_status: "ok" })).toBe("ok");
    expect(jobState({ last_status: "??" })).toBe("none");
  });
  it("데몬 문구", () => {
    expect(daemonText(null)).toEqual({ text: "확인 중", level: "none" });
    expect(daemonText(base)).toEqual({ text: "실행 중 · 2시간째", level: "ok" });
    expect(daemonText({ ...base, stale: true, age_s: 1200 })).toEqual({ text: "응답 없음 · 마지막 틱 20분 전", level: "warn" });
    expect(daemonText({ ...base, alive: false, stopped_at: "x" })).toEqual({ text: "멈춤 (정상 종료)", level: "err" });
    expect(daemonText({ ...base, alive: false, updated_at: null })).toEqual({ text: "실행 기록 없음", level: "err" });
  });
  it("'지금 실행' 요청은 스캔·학습·복기·체결만", () => {
    expect(runNowRequest({ id: "kr_scan_analyze", kind: "scan_analyze", market: "KR" })).toEqual({ kind: "scan_analyze", params: { market: "KR" } });
    expect(runNowRequest({ id: "ml_train", kind: "ml_train", market: null })).toEqual({ kind: "ml_train", params: {} });
    expect(runNowRequest({ id: "kr_monitor", kind: "monitor", market: "KR" })).toBeNull();
    expect(runNowRequest({ id: "job_queue", kind: "job_queue", market: null })).toBeNull();
  });
  it("버튼 활성 조건: 토큰 → 데몬", () => {
    expect(canRun(base, null).ok).toBe(false);
    expect(canRun(null, "t").reason).toContain("데몬");
    expect(canRun({ alive: false }, "t").ok).toBe(false);
    expect(canRun(base, "t")).toEqual({ ok: true });
  });
  it("요청 라벨·행 변환", () => {
    const req = (o: Partial<JobRequest>): JobRequest => ({
      id: 1, kind: "ml_train", params: {}, status: "queued", source: "web", requested_at: "2026-10-24T01:00:00+00:00",
      started_at: null, finished_at: null, progress: null, error: null, ...o,
    });
    expect(requestLabel(req({ kind: "analyze_ticker", params: { ticker: "005930", dry_run: true } }))).toBe("종목 분석 005930 (모의)");
    expect(requestLabel(req({ kind: "features_backfill", params: { years: 3 } }))).toBe("피처 백필 3년");
    expect(requestLabel(req({ label: "서버 라벨" }))).toBe("서버 라벨");
    const rows = requestRows([
      req({ id: 1, status: "running", started_at: "s", progress: "[3] KR 005930" }),
      req({ id: 2, status: "error", finished_at: "f", error: "폭발" }),
      req({ id: 3 }),
      req({ id: 4, kind: "reevaluate", priority: 10 }),
    ]);
    expect(rows.map((r) => [r.stateLabel, r.detail, r.cancellable, r.when])).toEqual([
      ["실행 중", "[3] KR 005930", false, "s"],
      ["오류", "폭발", false, "f"],
      ["대기", null, true, "2026-10-24T01:00:00+00:00"],
      ["대기", null, true, "2026-10-24T01:00:00+00:00"],
    ]);
    expect(rows.map((r) => r.priority)).toEqual([false, false, false, true]);
  });
});

describe("pollsJobs", () => {
  it("활성 요청이 있고 SSE 리더 탭일 때만 폴링한다(다른 탭은 중계를 받는다)", () => {
    expect(pollsJobs(true, true)).toBe(true);
    expect(pollsJobs(true, false)).toBe(false);
    expect(pollsJobs(false, true)).toBe(false);
  });
});
