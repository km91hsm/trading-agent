// 종목명: 서버 /api/names 가 준 {"KR:005930": "삼성전자"} 매핑의 키 규칙과 요청 묶기(순수 함수).
// 화면 표기는 Ticker 컴포넌트 하나가 맡는다 ("삼성전자 005930", 모르면 코드만).

export type NameMap = Record<string, string>;

export function nameKey(market: string, ticker: string): string {
  return `${market}:${ticker}`;
}

/** 아직 모르고(known) 요청한 적도 없는(requested) 키만 고른다. */
export function missingKeys(known: NameMap, requested: Set<string>, market: string, tickers: string[]): string[] {
  const out: string[] = [];
  for (const t of tickers) {
    if (!t) continue;
    const k = nameKey(market, t);
    if (known[k] !== undefined || requested.has(k) || out.includes(k)) continue;
    out.push(k);
  }
  return out;
}

/** "KR:005930" 들을 시장별 ticker 목록으로 — 시장마다 /api/names 1회. */
export function groupByMarket(keys: Iterable<string>): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const k of keys) {
    const i = k.indexOf(":");
    if (i <= 0) continue;
    const m = k.slice(0, i);
    (out[m] ??= []).push(k.slice(i + 1));
  }
  return out;
}

/** 표기 한 줄 (텍스트 문맥용: 제목·확인 대화상자). */
export function labelOf(names: NameMap, market: string, ticker: string): string {
  const n = names[nameKey(market, ticker)];
  return n && n !== ticker ? `${n} ${ticker}` : ticker;
}
