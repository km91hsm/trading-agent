import { useState } from "react";
import { getJson, postJson, withAccount } from "../api";
import { ApprovalsTable, ErrorBox, OrdersTable, RiskChecksTable } from "../components/Tables";
import { useApp, useLoad } from "../hooks";
import type { Approval, Order, RiskCheck } from "../types";

export function OrdersPage() {
  const { accountId, accounts, token, tick } = useApp();
  const [msg, setMsg] = useState<string | null>(null);
  const approvals = useLoad(
    () => (accountId ? getJson<Approval[]>(withAccount("/api/approvals", accountId)) : null),
    [accountId, tick],
  );
  const orders = useLoad(() => (accountId ? getJson<Order[]>(withAccount("/api/orders", accountId)) : null), [
    accountId,
    tick,
  ]);
  const checks = useLoad(
    () => (accountId ? getJson<RiskCheck[]>(withAccount("/api/risk-checks", accountId)) : null),
    [accountId, tick],
  );
  const decide = async (action: "approve" | "reject", id: number) => {
    const acct = accounts.find((a) => a.id === accountId);
    if (action === "approve" && acct?.env === "real" && !window.confirm(`실전 계좌 ${acct.id} 주문을 승인합니다. 계속할까요?`)) {
      return;
    }
    try {
      const r = await postJson<{ ok: boolean; order?: { order_no?: string } }>(
        `/api/control/${action}`,
        { approval_id: id },
        token,
      );
      setMsg(action === "approve" ? `승인 #${id} → 접수번호 ${r.order?.order_no ?? "-"}` : `거부 #${id}`);
      approvals.reload();
      orders.reload();
    } catch (e) {
      setMsg(`${action} 실패: ${(e as Error).message}`);
    }
  };
  return (
    <section>
      <h1>주문/승인</h1>
      <div className="row">
        {!token && <span className="muted">승인·거부는 설정에서 토큰을 넣어야 합니다</span>}
        {msg && <span>{msg}</span>}
      </div>
      <h2>승인 요청</h2>
      <ErrorBox error={approvals.error} />
      <ApprovalsTable
        rows={approvals.data ?? []}
        onApprove={token ? (id) => decide("approve", id) : undefined}
        onReject={token ? (id) => decide("reject", id) : undefined}
      />
      <h2>주문</h2>
      <ErrorBox error={orders.error} />
      <OrdersTable rows={orders.data ?? []} />
      <h2>리스크 판정</h2>
      <ErrorBox error={checks.error} />
      <RiskChecksTable rows={checks.data ?? []} />
    </section>
  );
}
