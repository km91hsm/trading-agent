import { describe, expect, it } from "vitest";
import { daemonText, jobLabel, jobState, scheduleText, type DaemonStatus } from "./jobs";

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
});
