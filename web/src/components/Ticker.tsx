// 종목 표기 한 곳: "삼성전자 005930 · KR". 이름은 앱 상태(names)에서 읽고 없으면 요청을 모아 둔다.
import { useContext, useEffect } from "react";
import { AppContext } from "../hooks";
import { nameKey } from "../names";

export function Ticker({ market, ticker, name, showMarket = true }: { market: string; ticker: string; name?: string | null; showMarket?: boolean }) {
  const app = useContext(AppContext); // 테스트(정적 렌더)에서는 컨텍스트 없이도 그린다
  const known = name ?? app?.names[nameKey(market, ticker)] ?? null;
  useEffect(() => {
    if (!known && app) app.ensureNames(market, [ticker]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [market, ticker, known]);
  const nm = known && known !== ticker ? known : null;
  const tail = [ticker !== nm ? ticker : null, showMarket ? market : null].filter(Boolean).join(" · ");
  return (
    <span className="nm" title={nm ? `${nm} ${ticker}` : ticker}>
      {nm ?? ticker}
      {nm || showMarket ? <small>{nm ? tail : market}</small> : null}
    </span>
  );
}
