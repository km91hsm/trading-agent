// 원장 표 네 개 (포지션·주문·승인·리스크 판정) — 시세표 열 문법(Table 원시요소)으로.
import { useMemo } from "react";
import { fmtDate, fmtDelta, fmtMoney, fmtTime, minutesLeft } from "../format";
import { planCells } from "../overview";
import type { Approval, Order, Position, RiskCheck } from "../types";
import { Badge } from "./Badge";
import { EmptyState } from "./EmptyState";
import { Delta, Num, Table, Td, Th } from "./Table";
import { Term } from "./Term";
import { Ticker } from "./Ticker";

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

const HORIZON_LABEL: Record<string, string> = { short: "단기", mid: "중기", long: "장기" };

/** 보유기간 프로필 표기: "단기 5일" / 결정 없는 보유분은 "—". */
export function horizonText(p: Pick<Position, "horizon" | "horizon_days">): string {
  if (!p.horizon) return "—";
  const label = HORIZON_LABEL[p.horizon] ?? p.horizon;
  return p.horizon_days ? `${label} ${p.horizon_days}일` : label;
}

/** 드리프트(%p) 표기: "+6.2%p" · 밴드 밖이면 주의 색. */
export function driftCell(d: number | null | undefined, bandPct = 0.05): { text: string; over: boolean } {
  if (d === null || d === undefined) return { text: "—", over: false };
  return { text: `${d > 0 ? "+" : ""}${d.toFixed(1)}%p`, over: Math.abs(d) > bandPct * 100 };
}

export function PositionsTable({ rows, nav, showAccounts = false, bandPct = 0.05 }: { rows: Position[]; nav?: string | null; showAccounts?: boolean; bandPct?: number }) {
  if (!rows.length) return <EmptyState title="보유 종목이 없어요" hint="매수가 체결되면 다음 판정 때 스냅샷에 잡혀요." />;
  const navN = nav ? Number(nav) : NaN;
  return (
    <Table responsive caption="보유 종목">
      <thead>
        <tr>
          <Th core>종목</Th>
          <Th>보유기간</Th>
          <Th num>수량</Th>
          <Th num>평균단가</Th>
          <Th num core>현재가</Th>
          <Th num core>평단 대비</Th>
          <Th num>평가금액</Th>
          <Th num core>비중</Th>
          <Th num>목표 비중</Th>
          <Th num>드리프트</Th>
          <Th num>익절가</Th>
          <Th num>손절가</Th>
          <Th num>지정가</Th>
          {showAccounts && <Th>계좌</Th>}
        </tr>
      </thead>
      <tbody>
        {rows.map((p) => {
          const d = fmtDelta(p.current_price, p.avg_price);
          const ev = p.eval_amount ? Number(p.eval_amount) : NaN;
          const plan = planCells(p.plan, p.market);
          return (
            <tr key={`${p.market}:${p.ticker}`}>
              <Td core>
                <Ticker market={p.market} ticker={p.ticker} />
              </Td>
              <Td>{horizonText(p)}</Td>
              <Td num>{p.qty}</Td>
              <Td num>
                <Num value={p.avg_price} market={p.market} />
              </Td>
              <Td num core>
                <Num value={p.current_price} market={p.market} />
              </Td>
              <Td num core>{d ? <Delta value={d.pct} pct /> : "—"}</Td>
              <Td num>
                <Num value={p.eval_amount} market={p.market} />
              </Td>
              <Td num core>{Number.isFinite(navN) && navN > 0 && Number.isFinite(ev) ? `${((ev / navN) * 100).toFixed(1)}%` : "—"}</Td>
              <Td num>{plan?.target ?? "—"}</Td>
              <Td num className={driftCell(p.drift_pp, bandPct).over ? "pend" : ""}>{driftCell(p.drift_pp, bandPct).text}</Td>
              <Td num className={plan?.tpClass}>{plan?.takeProfit ?? "—"}</Td>
              <Td num className={plan?.stopClass}>{plan?.stop ?? "—"}</Td>
              <Td num>{plan?.limit ?? "—"}</Td>
              {showAccounts && <Td>{(p.accounts ?? []).join(", ")}</Td>}
            </tr>
          );
        })}
      </tbody>
    </Table>
  );
}

export function OrdersTable({ rows, onTrace, traceId }: { rows: Order[]; onTrace?: (o: Order) => void; traceId?: number | null }) {
  if (!rows.length) return <EmptyState title="주문이 없어요" hint="결정이 매수·매도로 나고 리스크 게이트를 통과하면 쌓여요." />;
  return (
    <Table dense responsive caption="주문">
      <thead>
        <tr>
          {onTrace && <Th>추적</Th>}
          <Th>날짜</Th>
          <Th>시각</Th>
          <Th core>종목</Th>
          <Th core>방향</Th>
          <Th num>수량</Th>
          <Th num>가격</Th>
          <Th num core>금액</Th>
          <Th core>상태</Th>
          <Th>모드</Th>
          <Th>접수번호</Th>
          <Th>메시지</Th>
        </tr>
      </thead>
      <tbody>
        {rows.map((o) => (
          <tr key={o.id} data-status={o.status}>
            {onTrace && (
              <Td>
                {o.decision_id ? (
                  <button type="button" className={o.decision_id === traceId ? "btn btn-sm sel" : "btn btn-sm btn-ghost"} onClick={() => onTrace(o)} title="애널리스트 → 토론 → 결정 → 게이트 → 주문">
                    #{o.decision_id}
                  </button>
                ) : (
                  <span className="muted">—</span>
                )}
              </Td>
            )}
            <Td>{fmtDate(o.created_at)}</Td>
            <Td>{fmtTime(o.created_at, o.market)}</Td>
            <Td core>
              <Ticker market={o.market} ticker={o.ticker} />
            </Td>
            <Td core>{o.side}</Td>
            <Td num>{o.qty}</Td>
            <Td num>{o.price ? <Num value={o.price} market={o.market} /> : "시장가"}</Td>
            <Td num core>
              <Num value={o.amount} market={o.market} />
            </Td>
            <Td core>
              <Badge kind="status" value={o.status} />
            </Td>
            <Td>
              <Badge kind="mode" value={o.mode} />
            </Td>
            <Td className="muted">{o.order_no ?? "—"}</Td>
            <Td className="muted wrap">{o.message}</Td>
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
  now,
}: {
  rows: Approval[];
  onApprove?: (a: Approval) => void;
  onReject?: (a: Approval) => void;
  realAccounts?: string[];
  now?: Date;
}) {
  const at = useMemo(() => now ?? new Date(), [now]);
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
          const left = minutesLeft(a.expires_at, at);
          const m = a.order.market ?? "KR";
          return (
            <tr key={a.id} data-env={real ? "real" : "paper"}>
              <Td>{a.id}</Td>
              <Td>
                {a.account_id} <Badge kind="env" value={real ? "real" : "paper"} />
              </Td>
              <Td>
                <Ticker market={m} ticker={a.order.ticker ?? "—"} />
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
              <Ticker market={r.market ?? "KR"} ticker={r.ticker ?? "—"} />
            </Td>
            <Td>{r.action ? <Badge kind="action" value={r.action} /> : "—"}</Td>
            <Td className={r.approved ? "gate ok" : r.action === "HOLD" ? "gate muted" : "gate no"}>{r.approved ? `승인${r.adjusted_qty ? ` · ${r.adjusted_qty}주` : ""}` : r.action === "HOLD" ? "보유 · 주문 없음" : `거부 · ${r.reasons[0] ?? "사유 없음"}`}</Td>
            <Td num>{r.adjusted_qty ?? "—"}</Td>
            <Td className="muted wrap">{r.reasons.join(" / ") || "—"}</Td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}
