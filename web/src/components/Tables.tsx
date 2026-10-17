import type { Approval, Order, Position, RiskCheck } from "../types";

export function ErrorBox({ error }: { error: string | null }) {
  return error ? <div className="error">오류: {error}</div> : null;
}

export function PositionsTable({ rows }: { rows: Position[] }) {
  if (!rows.length) return <p className="muted">보유 종목 없음</p>;
  return (
    <table>
      <thead>
        <tr>
          <th>종목</th><th>수량</th><th>평균단가</th><th>현재가</th><th>평가금액</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((p) => (
          <tr key={p.ticker}>
            <td>{p.ticker}</td><td>{p.qty}</td><td>{p.avg_price}</td><td>{p.current_price}</td>
            <td>{p.eval_amount}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function OrdersTable({ rows }: { rows: Order[] }) {
  if (!rows.length) return <p className="muted">주문 없음</p>;
  return (
    <table>
      <thead>
        <tr>
          <th>시각(UTC)</th><th>종목</th><th>방향</th><th>수량</th><th>가격</th><th>상태</th>
          <th>접수번호</th><th>모드</th><th>메시지</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((o) => (
          <tr key={o.id} className={o.status}>
            <td>{o.created_at.slice(0, 19)}</td><td>{o.market} {o.ticker}</td><td>{o.side}</td>
            <td>{o.qty}</td><td>{o.price ?? "시장가"}</td><td>{o.status}</td>
            <td>{o.order_no ?? "-"}</td><td>{o.mode}</td><td className="muted">{o.message}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function ApprovalsTable({
  rows,
  onApprove,
  onReject,
}: {
  rows: Approval[];
  onApprove?: (id: number) => void;
  onReject?: (id: number) => void;
}) {
  if (!rows.length) return <p className="muted">승인 요청 없음</p>;
  return (
    <table>
      <thead>
        <tr>
          <th>#</th><th>계좌</th><th>주문</th><th>상태</th><th>요청</th><th>만료</th>
          {onApprove && <th>처리</th>}
        </tr>
      </thead>
      <tbody>
        {rows.map((a) => (
          <tr key={a.id}>
            <td>{a.id}</td><td>{a.account_id}</td>
            <td>
              {a.order.side} {a.order.market} {a.order.ticker} {a.order.qty}주 @{a.order.price ?? "시장가"}
            </td>
            <td>{a.status}</td><td>{a.requested_at.slice(0, 19)}</td><td>{a.expires_at.slice(0, 19)}</td>
            {onApprove && onReject && (
              <td>
                {a.status === "pending" && (
                  <>
                    <button onClick={() => onApprove(a.id)}>승인</button>{" "}
                    <button className="danger" onClick={() => onReject(a.id)}>거부</button>
                  </>
                )}
              </td>
            )}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function RiskChecksTable({ rows }: { rows: RiskCheck[] }) {
  if (!rows.length) return <p className="muted">판정 없음</p>;
  return (
    <table>
      <thead>
        <tr>
          <th>날짜</th><th>종목</th><th>결정</th><th>승인</th><th>수량</th><th>사유</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.id}>
            <td>{r.date}</td><td>{r.market} {r.ticker}</td><td>{r.action}</td>
            <td>{r.approved ? "예" : "아니오"}</td><td>{r.adjusted_qty ?? "-"}</td>
            <td className="muted">{r.reasons.join(" / ")}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
