import { useState } from "react";
import { AccountPicker } from "../components/AccountPicker";
import { getJson, postJson, withAccount } from "../api";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { Section } from "../components/Section";
import { Skeleton } from "../components/Skeleton";
import { ApprovalsTable, ErrorBox, OrdersTable, RiskChecksTable } from "../components/Tables";
import { useToast } from "../components/Toast";
import { fmtMoney } from "../format";
import { useApp, useLoad } from "../hooks";
import { labelOf } from "../names";
import type { Approval, Order, RiskCheck } from "../types";
import { limitDistancePct, type TraceResp, traceSteps, unknownLine } from "../trace";
import { CsvLink } from "../components/CsvLink";

export function OrdersPage() {
  const { accountId, accounts, token, tick, names } = useApp();
  const toast = useToast();
  const acct = accounts.find((a) => a.id === accountId);
  const [pendingAct, setPendingAct] = useState<{ action: "approve" | "reject"; a: Approval } | null>(null);
  const approvals = useLoad(() => (accountId ? getJson<Approval[]>(withAccount("/api/approvals", accountId)) : null), [accountId, tick]);
  const orders = useLoad(() => (accountId ? getJson<Order[]>(withAccount("/api/orders", accountId)) : null), [accountId, tick]);
  const checks = useLoad(() => (accountId ? getJson<RiskCheck[]>(withAccount("/api/risk-checks", accountId)) : null), [accountId, tick]);

  const run = async (action: "approve" | "reject", a: Approval) => {
    try {
      const r = await postJson<{ ok: boolean; order?: { order_no?: string } }>(`/api/control/${action}`, { approval_id: a.id }, token);
      toast(action === "approve" ? `승인 #${a.id} → 접수번호 ${r.order?.order_no ?? "—"}` : `거부 #${a.id}`, "ok");
      approvals.reload();
      orders.reload();
    } catch (e) {
      toast(`${action === "approve" ? "승인" : "거부"} 실패: ${(e as Error).message}`, "error");
    }
  };
  const decide = (action: "approve" | "reject", a: Approval) => {
    // 실전 계좌의 승인은 한 번 더 묻는다. 거부·모의는 바로.
    if (action === "approve" && acct?.env === "real") setPendingAct({ action, a });
    else void run(action, a);
  };
  const pending = (approvals.data ?? []).filter((a) => a.status === "pending");
  const realIds = accounts.filter((a) => a.env === "real").map((a) => a.id);
  const [traceId, setTraceId] = useState<number | null>(null);
  const trace = useLoad(() => (traceId ? getJson<TraceResp>(`/api/decisions/${traceId}/trace`) : null), [traceId]);
  const warn = unknownLine(orders.data ?? []);

  if (!accountId)
    return (
      <>
        <header className="page-h">
          <h1>주문/승인</h1>
        </header>
        <AccountPicker purpose="주문·승인" />
      </>
    );
  return (
    <>
      <header className="page-h">
        <h1>주문/승인</h1>
        <p className="sub">
          승인 요청은 30분 뒤 만료되고 한 번만 실행돼요.{" "}
          {!token && <span className="pend-text">승인·거부하려면 설정/제어에서 토큰을 넣으세요.</span>}
        </p>
      </header>
      <Section title="승인 대기" aside={`${pending.length}건`}>
        <ErrorBox error={approvals.error} />
        {approvals.loading && !approvals.data ? (
          <Skeleton rows={2} cols={6} />
        ) : (
          <ApprovalsTable
            rows={approvals.data ?? []}
            realAccounts={realIds}
            onApprove={token ? (a) => decide("approve", a) : undefined}
            onReject={token ? (a) => decide("reject", a) : undefined}
          />
        )}
      </Section>
      <Section
        title="주문"
        aside={
          <span className="row tight">
            {`${orders.data?.length ?? 0}건 · 최신순`}
            {accountId && <CsvLink href={withAccount("/api/orders", accountId)} />}
          </span>
        }
      >
        <ErrorBox error={orders.error} />
        {warn && <p className="pend-text">{warn}</p>}
        {orders.loading && !orders.data ? <Skeleton rows={4} cols={8} /> : <OrdersTable rows={orders.data ?? []} onTrace={(o) => setTraceId(o.decision_id === traceId ? null : o.decision_id)} traceId={traceId} />}
        {traceId && (
          <details open className="trace">
            <summary>
              결정 #{traceId} 판단 과정{trace.data ? ` · ${trace.data.decision.ticker} ${trace.data.decision.date}` : ""}
            </summary>
            <ErrorBox error={trace.error} />
            {trace.data && (
              <dl className="trace-steps">
                {traceSteps(trace.data).map((s) => (
                  <div key={s.key}>
                    <dt>{s.title}</dt>
                    {s.lines.length === 0 ? <dd className="muted">—</dd> : s.lines.map((l) => <dd key={l}>{l}</dd>)}
                  </div>
                ))}
                {trace.data.quote && (
                  <div>
                    <dt>지정가 거리</dt>
                    {(orders.data ?? [])
                      .filter((o) => o.decision_id === traceId && o.price)
                      .map((o) => {
                        const d = limitDistancePct(o.price, trace.data?.quote);
                        return <dd key={o.id}>#{o.id} {o.side} @{o.price}: 당시 현재가 대비 {d === null ? "—" : `${d >= 0 ? "+" : ""}${d.toFixed(2)}%`}</dd>;
                      })}
                  </div>
                )}
              </dl>
            )}
          </details>
        )}
      </Section>
      <Section title="리스크 판정" aside="거부 사유는 전부 기록">
        <ErrorBox error={checks.error} />
        {checks.loading && !checks.data ? <Skeleton rows={4} cols={6} /> : <RiskChecksTable rows={checks.data ?? []} />}
      </Section>
      <ConfirmDialog
        open={pendingAct !== null}
        title="실전 계좌 주문을 승인합니다"
        body={
          pendingAct ? (
            <>
              <b>{pendingAct.a.account_id}</b> · {pendingAct.a.order.market} {labelOf(names, pendingAct.a.order.market ?? "KR", pendingAct.a.order.ticker ?? "")} {pendingAct.a.order.side}{" "}
              {pendingAct.a.order.qty}주 @ {pendingAct.a.order.price ? fmtMoney(pendingAct.a.order.price, pendingAct.a.order.market ?? "KR") : "시장가"}
              <br />
              실제 돈이 나갑니다. 승인하면 바로 브로커에 주문합니다.
            </>
          ) : null
        }
        confirmLabel="실전 주문 승인"
        danger
        onConfirm={() => {
          const p = pendingAct;
          setPendingAct(null);
          if (p) void run(p.action, p.a);
        }}
        onClose={() => setPendingAct(null)}
      />
    </>
  );
}
