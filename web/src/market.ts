// 국내 실시간 지수(상단 바)·호가 10단(차트) — 서버 웹소켓 허브(`/api/quotes/indices`·`/book`)를 그리는 순수 함수.
export type IndexQuote = { price: string; change_pct: number | null; asof: string };
export type IndicesResp = { session: "open" | "closed"; asof: string; indices: Record<string, IndexQuote> };
export type Book = { asks: string[][]; bids: string[][]; total_ask: string; total_bid: string; time: string; asof: string };
export type BookResp = { session: "open" | "closed"; ticker: string; book: Book | null };

export const INDEX_ORDER = ["KOSPI", "KOSDAQ"] as const;

/** 지수 칩: 이름·값(소수 2자리, 천 단위)·등락률(부호). 방향은 up/dn 클래스. */
export function indexChips(r: IndicesResp | null): Array<{ name: string; value: string; pct: string; dir: "up" | "dn" | "flat" }> {
  if (!r || r.session !== "open") return [];
  return INDEX_ORDER.flatMap((name) => {
    const q = r.indices[name];
    if (!q) return [];
    const v = Number(q.price);
    const pct = q.change_pct ?? 0;
    return [
      {
        name,
        value: Number.isFinite(v) ? v.toLocaleString("ko-KR", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : q.price,
        pct: `${pct > 0 ? "+" : ""}${(pct * 100).toFixed(2)}%`,
        dir: pct > 0 ? "up" : pct < 0 ? "dn" : "flat",
      },
    ];
  });
}

/** 호가 표 행: 위에서부터 매도 10(높은 가격 먼저) → 매수 10. 잔량 막대 폭은 최대 잔량 대비 %. */
export function bookRows(b: Book | null): Array<{ side: "ask" | "bid"; price: string; qty: number; barPct: number }> {
  if (!b) return [];
  const asks = b.asks.map(([p, q]) => ({ side: "ask" as const, price: p, qty: Number(q) || 0 })).reverse();
  const bids = b.bids.map(([p, q]) => ({ side: "bid" as const, price: p, qty: Number(q) || 0 }));
  const rows = [...asks, ...bids].filter((r) => Number(r.price) > 0);
  const max = Math.max(1, ...rows.map((r) => r.qty));
  return rows.map((r) => ({ ...r, barPct: Math.round((r.qty / max) * 100) }));
}

/** 다음 폴링: 장중 값이 오면 1·2초, 장중인데 아직 없으면 5초, 장 밖 60초. */
export function marketPollMs(session: string | undefined, hasData: boolean, fastMs: number): number {
  if (session !== "open") return 60_000;
  return hasData ? fastMs : 5_000;
}
