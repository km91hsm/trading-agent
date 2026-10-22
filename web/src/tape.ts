// 대시보드 순수 함수: 장 상태 라벨, 오늘의 시세표 행 조립. 화면과 분리해 vitest 로 고정한다.

export type Phase = "pre" | "open" | "closed" | "weekend";

const PHASE_LABEL: Record<Phase, string> = { pre: "개장 전", open: "정규장", closed: "마감", weekend: "휴장" };

function localParts(d: Date, tz: string): { weekday: number; minutes: number } {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: tz,
    weekday: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).formatToParts(d);
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? "";
  const wd = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].indexOf(get("weekday"));
  const h = Number(get("hour")) % 24;
  const m = Number(get("minute"));
  return { weekday: wd, minutes: h * 60 + m };
}

function phase(d: Date, tz: string, open: number, close: number): Phase {
  const { weekday, minutes } = localParts(d, tz);
  if (weekday === 0 || weekday === 6) return "weekend";
  if (minutes < open) return "pre";
  if (minutes < close) return "open";
  return "closed";
}

/** KR 09:00~15:30 KST, US 09:30~16:00 ET (휴장일은 모른다 — 주말만 본다). */
export function marketPhase(now: Date): { kr: Phase; us: Phase; label: string } {
  const kr = phase(now, "Asia/Seoul", 9 * 60, 15 * 60 + 30);
  const us = phase(now, "America/New_York", 9 * 60 + 30, 16 * 60);
  return { kr, us, label: `KR ${PHASE_LABEL[kr]} · US ${PHASE_LABEL[us]}` };
}
