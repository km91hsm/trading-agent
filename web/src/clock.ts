// 상단 바 시계: 페이지를 켜 둔 동안 1초마다 흐른다(useNow). 표기만 순수 함수로 둔다.
import { marketPhase } from "./tape";

const parts = (d: Date, timeZone: string, opts: Intl.DateTimeFormatOptions) =>
  new Intl.DateTimeFormat("en-GB", { timeZone, hour12: false, ...opts }).format(d);

const WEEKDAY = ["일", "월", "화", "수", "목", "금", "토"];

export type ClockText = { date: string; kst: string; kstShort: string; et: string; phase: string };

/** {date: "10.26 (월)", kst: "21:06:05", kstShort: "21:06", et: "08:06", phase: "KR 마감 · US 개장 전"} */
export function clockText(now: Date): ClockText {
  const ymd = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Seoul",
    month: "2-digit",
    day: "2-digit",
  }).format(now); // "10-26"
  const dow = new Intl.DateTimeFormat("en-US", { timeZone: "Asia/Seoul", weekday: "short" }).format(now);
  const wd = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].indexOf(dow);
  return {
    date: `${ymd.replace("-", ".")} (${WEEKDAY[wd] ?? ""})`,
    kst: parts(now, "Asia/Seoul", { hour: "2-digit", minute: "2-digit", second: "2-digit" }),
    kstShort: parts(now, "Asia/Seoul", { hour: "2-digit", minute: "2-digit" }),
    et: parts(now, "America/New_York", { hour: "2-digit", minute: "2-digit" }),
    phase: marketPhase(now).label,
  };
}
