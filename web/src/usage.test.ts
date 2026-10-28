import { describe, expect, it } from "vitest";
import { agoText, barPct, observationStale, resetsIn, sourceText, throttleSetting, throttleText, usageLevel } from "./usage";

describe("usage", () => {
  it("agoText", () => {
    expect(agoText(null)).toBe("관측 없음");
    expect(agoText(10)).toBe("방금");
    expect(agoText(180)).toBe("3분 전");
    expect(agoText(7200)).toBe("2시간 전");
    expect(agoText(3 * 86400)).toBe("3일 전");
  });
  it("barPct 는 0~100 으로 자른다", () => {
    expect(barPct(null)).toBe(0);
    expect(barPct(12.4)).toBe(12.4);
    expect(barPct(140)).toBe(100);
    expect(barPct(-3)).toBe(0);
  });
  it("usageLevel 단계", () => {
    expect(usageLevel(null)).toBe("none");
    expect(usageLevel(10)).toBe("ok");
    expect(usageLevel(75)).toBe("warn");
    expect(usageLevel(95)).toBe("danger");
  });
  it("resetsIn", () => {
    const now = new Date("2026-10-23T03:00:00Z");
    expect(resetsIn(null, now)).toBeNull();
    expect(resetsIn("2026-10-23T02:00:00Z", now)).toBeNull();
    expect(resetsIn("2026-10-23T03:30:00Z", now)).toBe("30분 뒤 리셋");
    expect(resetsIn("2026-10-23T05:10:00Z", now)).toBe("2시간 10분 뒤 리셋");
    expect(resetsIn("2026-10-27T03:00:00Z", now)).toBe("4일 뒤 리셋");
  });
  it("관측이 6시간 넘으면 stale", () => {
    expect(observationStale(3600)).toBe(false);
    expect(observationStale(7 * 3600)).toBe(true);
    expect(observationStale(null)).toBe(false);
  });
});

describe("관측 출처·감속 문구 (Phase 24)", () => {
  it("출처와 감속 상태", () => {
    expect(sourceText("session")).toBe("분석 세션이 받은 값");
    expect(throttleText({ enabled: true, soft_pct: 0.8, hard_pct: 0.95, state: "soft", reason: null })).toBe("감속: 보유 재평가만 (80% 이상)");
    expect(throttleText({ enabled: true, soft_pct: 0.8, hard_pct: 0.95, state: "hard", reason: null })).toBe("감속: 새 세션 전부 중지 (95% 이상)");
    expect(throttleText({ enabled: true, soft_pct: 0.8, hard_pct: 0.95, state: "ok", reason: null })).toBe("");
  });
});

import { ageSince, resetLine } from "./usage";

describe("resetLine·ageSince", () => {
  const now = new Date("2026-10-23T03:00:00Z"); // 12:00 KST
  it("초기화 시각을 KST 절대 시각 + 남은 시간으로, 지나도 사라지지 않는다", () => {
    expect(resetLine("2026-10-23T05:10:00Z", now)).toBe("14:10 초기화 · 2시간 10분 남음");
    expect(resetLine("2026-10-23T03:30:00Z", now)).toBe("12:30 초기화 · 30분 남음");
    expect(resetLine("2026-10-25T00:00:00Z", now)).toBe("10.25 09:00 초기화 · 45시간 0분 남음");
    expect(resetLine("2026-10-27T03:00:00Z", now)).toBe("10.27 12:00 초기화 · 4일 남음");
    expect(resetLine("2026-10-23T02:00:00Z", now)).toBe("11:00 초기화됨 · 다음 확인 때 새 창");
    expect(resetLine(null, now)).toBeNull();
  });
  it("관측 경과는 클라이언트 시계 기준", () => {
    expect(ageSince("2026-10-23T02:55:00Z", now)).toBe(300);
    expect(ageSince(null, now)).toBeNull();
  });
});

import { usageChip } from "./usage";

describe("usageChip", () => {
  it("관측 없는 창은 0% 대신 —", () => {
    const base = { observed_at: null, age_s: null, spend_limit: null };
    expect(usageChip({ ...base, five_hour: null, seven_day: { used_pct: 54, resets_at: null } })).toBe("5h — · 7d 54%");
    expect(usageChip({ ...base, five_hour: { used_pct: 12.4, resets_at: null }, seven_day: null })).toBe("5h 12% · 7d —");
  });
  it("throttleSetting: 켜짐·꺼짐·출처", () => {
    expect(throttleSetting({ enabled: true, source: "yaml", soft_pct: 0.8, hard_pct: 0.95, state: "ok", reason: null })).toBe(
      "켜짐 · 80% 부터 보유 재평가만, 95% 부터 새 세션 중지 · 설정 파일 값",
    );
    expect(throttleSetting({ enabled: false, source: "control", soft_pct: 0.8, hard_pct: 0.95, state: "ok", reason: null })).toBe(
      "꺼짐 · 사용률과 무관하게 세션을 띄움(하루 세션 상한은 그대로) · 설정 화면에서 바꿈",
    );
    expect(throttleSetting(undefined)).toBe("상태 모름");
  });
});
