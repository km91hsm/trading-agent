// 세금 로트·워시세일 (Phase 25, /api/tax-lots) — 표시 전용. 행 변환 순수 함수.
import { fmtMoney } from "./format";

export type RealizedLot = {
  ticker: string;
  qty: number;
  buy_date: string;
  sell_date: string;
  cost: string;
  proceeds: string;
  pnl: string;
  term: "short" | "long" | null;
  wash_sale: boolean;
};
export type TaxLotsResp = {
  account_id: string;
  market: string;
  year: number | null;
  realized: RealizedLot[];
  open_lots: Array<{ ticker: string; qty: number; price: string; bought_at: string }>;
  summary: { realized_pnl: string; short_pnl: string; long_pnl: string; wash_sales: number; unmatched_sell_qty: number };
  note: string;
};

const TERM: Record<string, string> = { short: "단기", long: "장기" };

export type LotRow = { key: string; ticker: string; qty: number; period: string; pnl: string; up: boolean | null; term: string; wash: boolean };

export function lotRows(r: TaxLotsResp | null | undefined): LotRow[] {
  if (!r) return [];
  return [...r.realized].reverse().map((x, i) => {
    const n = Number(x.pnl);
    return {
      key: `${x.ticker}:${x.sell_date}:${i}`,
      ticker: x.ticker,
      qty: x.qty,
      period: `${x.buy_date} → ${x.sell_date}`,
      pnl: fmtMoney(x.pnl, r.market),
      up: Number.isFinite(n) && n !== 0 ? n > 0 : null,
      term: x.term ? TERM[x.term] ?? x.term : "—",
      wash: x.wash_sale,
    };
  });
}

/** 머리 한 줄: "실현 −20원 · 단기 … · 장기 … · 워시세일 1건". KR 은 장단기 없음. */
export function lotsHeadline(r: TaxLotsResp | null | undefined): string {
  if (!r) return "";
  const s = r.summary;
  const parts = [`실현 ${fmtMoney(s.realized_pnl, r.market)}`];
  if (r.market === "US") parts.push(`단기 ${fmtMoney(s.short_pnl, r.market)}`, `장기 ${fmtMoney(s.long_pnl, r.market)}`);
  if (s.wash_sales) parts.push(`워시세일 ${s.wash_sales}건`);
  if (s.unmatched_sell_qty) parts.push(`원가 모름 ${s.unmatched_sell_qty}주`);
  return parts.join(" · ");
}
