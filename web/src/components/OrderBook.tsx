// 차트 화면 호가 10단(국내, 정규장, 서버 웹소켓 허브 임차 10초) — 시세표 열 문법, 잔량은 SVG 막대. 표시 전용.
import { usePolling } from "../hooks";
import { bookRows, marketPollMs, type BookResp } from "../market";
import { EmptyState } from "./EmptyState";
import { Table, Td, Th } from "./Table";

export function OrderBook({ ticker }: { ticker: string }) {
  const r = usePolling<BookResp>(`/api/quotes/book?ticker=${encodeURIComponent(ticker)}`, (x) =>
    marketPollMs(x?.session, !!x?.book, 1_000),
  );
  if (!r) return null;
  if (r.session !== "open") return <p className="note">호가는 국내 정규장에만 보여요.</p>;
  const rows = bookRows(r.book);
  if (!rows.length) return <EmptyState title="호가를 받는 중이에요" hint="웹소켓 구독이 붙으면 1초마다 바뀌어요." />;
  return (
    <>
      <Table dense caption="호가 10단">
        <thead>
          <tr>
            <Th num>매도 잔량</Th>
            <Th num>가격</Th>
            <Th num>매수 잔량</Th>
          </tr>
        </thead>
        <tbody>
          {rows.map((x) => (
            <tr key={`${x.side}${x.price}`} data-side={x.side}>
              <Td num>{x.side === "ask" ? <Depth qty={x.qty} pct={x.barPct} side="ask" /> : null}</Td>
              <Td num>
                <span className={x.side === "ask" ? "dn" : "up"}>{Number(x.price).toLocaleString("ko-KR")}</span>
              </Td>
              <Td num>{x.side === "bid" ? <Depth qty={x.qty} pct={x.barPct} side="bid" /> : null}</Td>
            </tr>
          ))}
        </tbody>
      </Table>
      <p className="note">
        총 매도 {Number(r.book?.total_ask ?? 0).toLocaleString("ko-KR")} · 총 매수 {Number(r.book?.total_bid ?? 0).toLocaleString("ko-KR")} · 주문·판정에는 쓰지 않아요.
      </p>
    </>
  );
}

function Depth({ qty, pct, side }: { qty: number; pct: number; side: "ask" | "bid" }) {
  return (
    <span className="depth">
      <svg className="bar-svg" viewBox="0 0 100 8" preserveAspectRatio="none" aria-hidden="true">
        <rect x={side === "ask" ? 100 - pct : 0} y="0" width={pct} height="8" fill="var(--ink-3)" />
      </svg>
      {qty.toLocaleString("ko-KR")}
    </span>
  );
}
