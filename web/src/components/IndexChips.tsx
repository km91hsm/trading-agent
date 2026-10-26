// 상단 바 KOSPI·KOSDAQ 실시간 지수(국내 정규장, 서버 웹소켓 허브). 장 밖이면 아무것도 그리지 않는다.
import { usePolling } from "../hooks";
import { indexChips, marketPollMs, type IndicesResp } from "../market";

export function IndexChips() {
  const r = usePolling<IndicesResp>("/api/quotes/indices", (x) => marketPollMs(x?.session, !!x && Object.keys(x.indices).length > 0, 2_000));
  const chips = indexChips(r);
  if (!chips.length) return null;
  return (
    <span className="index-chips" aria-label="국내 지수 실시간">
      {chips.map((c) => (
        <span key={c.name} className="index-chip">
          {c.name} <b>{c.value}</b> <span className={c.dir === "flat" ? undefined : c.dir}>{c.pct}</span>
        </span>
      ))}
    </span>
  );
}
