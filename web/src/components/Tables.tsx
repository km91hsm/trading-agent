// 원장 표 네 개 (포지션·주문·승인·리스크 판정) — 시세표 열 문법(Table 원시요소)으로.
import { fmtDate, fmtDelta, fmtMoney, fmtTime, minutesLeft } from "../format";
import type { Approval, Order, Position, RiskCheck } from "../types";
import { Badge } from "./Badge";
import { EmptyState } from "./EmptyState";
import { Delta, Num, Table, Td, Th } from "./Table";
import { Term } from "./Term";

export function ErrorBox({ error }: { error: string | null }) {
  if (!error) return null;
  const auth = /토큰|401/.test(error);
  return (
    <div className="error" role="alert">
      {auth ? "제어하려면 설정/제어에서 토큰을 넣으세요 — " : "오류: "}
      {error}
    </div>
  );
}

export function PositionsTable({ rows, nav }: { rows: Position[]; nav?: string | null }) {
  if (!rows.length) return <EmptyState title="보유 종목이 없어요" hint="매수가 체결되면 다음 판정 때 스냅샷에 잡혀요." />;
  const navN = nav ? Number(nav) : NaN;
  return (
    <Table caption="보유 종목">
      <thead>
        <tr>
          <Th>종목</Th>
          <Th num>수량</Th>
          <Th num>평균단가</Th>
          <Th num>현재가</Th>
          <Th num>평단 대비</Th>
          <Th num>평가금액</Th>
          <Th num>비중</Th>
        </tr>
      </thead>
      <tbody>
        {rows.map((p) => {
          const d = fmtDelta(p.current_price, p.avg_price);
          const ev = p.eval_amount ? Number(p.eval_amount) : NaN;
          return (
            <tr key={`${p.market}:${p.ticker}`}>
              <Td>
                <span className="nm">
                  {p.ticker}
                  <small>{p.market}</small>
                </span>
              </Td>
              <Td num>{p.qty}</Td>
              <Td num>
                <Num value={p.avg_price} market={p.market} />
              </Td>
              <Td num>
                <Num value={p.current_price} market={p.market} />
              </Td>
              <Td num>{d ? <Delta value={d.pct} pct /> : "—"}</Td>
              <Td num>
                <Num value={p.eval_amount} market={p.market} />
              </Td>
              <Td num>{Number.isFinite(navN) && navN > 0 && Number.isFinite(ev) ? `${((ev / navN) * 100).toFixed(1)}%` : "—"}</Td>
            </tr>
          );
        })}
      </tbody>
    </Table>
  );
}

export function OrdersTable({ rows }: { rows: Order[] }) {
  if (!rows.length) return <EmptyState title="주문이 없어요" hint="결정이 매수·매도로 나고 리스크 게이트를 통과하면 쌓여요." />;
  return (
    <Table dense caption="주문">
      <thead>
        <tr>
          <Th>날짜</Th>
          <Th>시각</Th>
          <Th>종목</Th>
          <Th>방향</Th>
          <Th num>수량</Th>
          <Th num>가격</Th>
          <Th num>금액</Th>
          <Th>상태</Th>
          <Th>모드</Th>
          <Th>접수번호</Th>
          <Th>메시지</Th>
        </tr>
      </thead>
      <tbody>
        {rows.map((o) => (
          <tr key={o.id} data-status={o.status}>
            <Td>{fmtDate(o.created_at)}</Td>
            <Td>{fmtTime(o.created_at, o.market)}</Td>
            <Td>
              <span className="nm">
                {o.ticker}
                <small>{o.market}</small>
              </span>
            </Td>
            <Td>{o.side}</Td>
            <Td num>{o.qty}</Td>
            <Td num>{o.price ? <Num value={o.price} market={o.market} /> : "시장가"}</Td>
            <Td num>
              <Num value={o.amount} market={o.market} />
            </Td>
            <Td>
              <Badge kind="status" value={o.status} />
            </Td>
            <Td>
              <Badge kind="mode" value={o.mode} />
            </Td>
            <Td className="muted">{o.order_no ?? "—"}</Td>
            <Td className="muted">{o.message}</Td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}

export function ApprovalsTable({
  rows,
  onApprove,
  onReject,
  realAccounts = [],
  now = new Date(),
}: {
  rows: Approval[];
  onApprove?: (a: Approval) => void;
  onReject?: (a: Approval) => void;
  realAccounts?: string[];
  now?: Date;
}) {
  if (!rows.length) return <EmptyState title="승인 요청이 없어요" hint="승인 모드 계좌의 주문이 게이트를 통과하면 30분 동안 여기서 기다려요." />;
  const canAct = !!(onApprove && onReject);
  return (
    <Table dense caption="승인 요청">
      <thead>
        <tr>
          <Th>#</Th>
          <Th>계좌</Th>
          <Th>종목</Th>
          <Th>방향</Th>
          <Th num>수량</Th>
          <Th num>가격</Th>
          <Th>상태</Th>
          <Th>요청</Th>
          <Th>만료</Th>
          {canAct && <Th>처리</Th>}
        </tr>
      </thead>
      <tbody>
        {rows.map((a) => {
          const real = realAccounts.includes(a.account_id);
          const left = minutesLeft(a.expires_at, now);
          const m = a.order.market ?? "KR";
          return (
            <tr key={a.id} data-env={real ? "real" : "paper"}>
              <Td>{a.id}</Td>
              <Td>
                {a.account_id} <Badge kind="env" value={real ? "real" : "paper"} />
              </Td>
              <Td>
                <span className="nm">
                  {a.order.ticker}
                  <small>{m}</small>
                </span>
              </Td>
              <Td>{a.order.side}</Td>
              <Td num>{a.order.qty}</Td>
              <Td num>{a.order.price ? fmtMoney(a.order.price, m) : "시장가"}</Td>
              <Td>
                <Badge kind="status" value={a.status} />
              </Td>
              <Td>
                {fmtDate(a.requested_at)} {fmtTime(a.requested_at, "KR")}
              </Td>
              <Td>{a.status === "pending" ? (left === null ? "—" : `${left}분 뒤`) : fmtTime(a.expires_at, "KR")}</Td>
              {canAct && (
                <Td>
                  {a.status === "pending" && (
                    <span className="row tight">
                      <button type="button" className="btn btn-sm btn-primary" onClick={() => onApprove?.(a)}>
                        승인
                      </button>
                      <button type="button" className="btn btn-sm btn-danger" onClick={() => onReject?.(a)}>
                        거부
                      </button>
                    </span>
                  )}
                </Td>
              )}
            </tr>
          );
        })}
      </tbody>
    </Table>
  );
}

export function RiskChecksTable({ rows }: { rows: RiskCheck[] }) {
  if (!rows.length) return <EmptyState title="리스크 판정이 없어요" hint="결정이 나면 계좌마다 한도 판정이 기록돼요." />;
  return (
    <Table dense caption="리스크 판정">
      <thead>
        <tr>
          <Th>날짜</Th>
          <Th>종목</Th>
          <Th>결정</Th>
          <Th>판정</Th>
          <Th num>수량</Th>
          <Th>
            사유 (<Term k="stance">전부 기록</Term>)
          </Th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.id}>
            <Td>{r.date ?? fmtDate(r.created_at)}</Td>
            <Td>
              <span className="nm">
                {r.ticker ?? "—"}
                <small>{r.market}</small>
              </span>
            </Td>
            <Td>{r.action ? <Badge kind="action" value={r.action} /> : "—"}</Td>
            <Td>{r.approved ? <Badge kind="ok" value="승인" /> : <Badge kind="status" value="rejected" />}</Td>
            <Td num>{r.adjusted_qty ?? "—"}</Td>
            <Td className="muted">{r.reasons.join(" / ") || "—"}</Td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}
