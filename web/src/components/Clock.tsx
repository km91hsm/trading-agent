// 상단 바 시계(한국·뉴욕 모두 날짜·요일·초 단위 + 장 상태). 이 컴포넌트만 1초마다 다시 그린다.
import { clockText } from "../clock";
import { useNow } from "../hooks";

export function Clock() {
  const now = useNow(1000);
  const t = clockText(now);
  return (
    <div className="clock" role="timer" aria-live="off" aria-label={`지금 ${t.date} ${t.kstShort} KST, 뉴욕 ${t.etDate} ${t.etShort}, ${t.phase}`}>
      <time className="clock-kst" dateTime={now.toISOString()}>
        <span className="clock-date">{t.date}</span> <b>{t.kst}</b> <span className="clock-tz">KST</span>
      </time>
      <span className="clock-et">
        <span className="clock-date">{t.etDate}</span> <b>{t.et}</b> <span className="clock-tz">뉴욕</span>
      </span>
      <span className="clock-phase">{t.phase}</span>
    </div>
  );
}
