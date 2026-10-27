// 상단 바 시계: 페이지를 켜 둔 동안 1초마다 흐른다(useNow). 표기만 순수 함수로 둔다.
import { marketPhase } from "./tape";

const parts = (d: Date, timeZone: string, opts: Intl.DateTimeFormatOptions) =>
  new Intl.DateTimeFormat("en-GB", { timeZone, hour12: false, ...opts }).format(d);

const WEEKDAY = ["일", "월", "화", "수", "목", "금", "토"];

export type ClockText = {
  date: string;
  kst: string;
  kstShort: string;
  etDate: string;
  et: string;
  etShort: string;
  phase: string;
};

/** "10.26 (월)" — 그 시간대의 날짜·요일(뉴욕은 한국보다 하루 늦을 때가 많다). */
function dateIn(now: Date, timeZone: string): string {
  const ymd = new Intl.DateTimeFormat("en-CA", { timeZone, month: "2-digit", day: "2-digit" }).format(now); // "10-26"
  const dow = new Intl.DateTimeFormat("en-US", { timeZone, weekday: "short" }).format(now);
  const wd = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].indexOf(dow);
  return `${ymd.replace("-", ".")} (${WEEKDAY[wd] ?? ""})`;
}

/** {date: "10.26 (월)", kst: "21:06:05", etDate: "10.26 (월)", et: "08:06:05", phase: "KR 마감 · US 개장 전"} —
 *  뉴욕도 한국처럼 날짜·요일·초까지(사용자 요청 2026-10-27). */
export function clockText(now: Date): ClockText {
  const hms: Intl.DateTimeFormatOptions = { hour: "2-digit", minute: "2-digit", second: "2-digit" };
  const hm: Intl.DateTimeFormatOptions = { hour: "2-digit", minute: "2-digit" };
  return {
    date: dateIn(now, "Asia/Seoul"),
    kst: parts(now, "Asia/Seoul", hms),
    kstShort: parts(now, "Asia/Seoul", hm),
    etDate: dateIn(now, "America/New_York"),
    et: parts(now, "America/New_York", hms),
    etShort: parts(now, "America/New_York", hm),
    phase: marketPhase(now).label,
  };
}
