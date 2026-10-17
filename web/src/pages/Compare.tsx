import { useState } from "react";
import { getJson } from "../api";
import { ErrorBox } from "../components/Tables";
import { useApp, useLoad } from "../hooks";
import type { Decision, Outcome } from "../types";

type Detail = { decision: Decision & { data: Record<string, unknown> }; outcomes: Outcome[] };

export function ComparePage() {
  const { tick } = useApp();
  const [sel, setSel] = useState<number | null>(null);
  const list = useLoad(() => getJson<Decision[]>("/api/decisions", { days: "30" }), [tick]);
  const detail = useLoad(() => (sel ? getJson<Detail>(`/api/decisions/${sel}/outcomes`) : null), [sel, tick]);
  return (
    <section>
      <h1>계좌 비교</h1>
      <p className="muted">같은 결정이 계좌마다 어떻게 판정·주문됐는지 본다 (모의 vs 실전).</p>
      <ErrorBox error={list.error} />
      <div className="split">
        <table>
          <thead>
            <tr>
              <th>날짜</th><th>시장</th><th>종목</th><th>결정</th><th>비중</th><th>확신</th>
            </tr>
          </thead>
          <tbody>
            {(list.data ?? []).map((d) => (
              <tr key={d.id} className={sel === d.id ? "sel" : ""} onClick={() => setSel(d.id)}>
                <td>{d.date}</td><td>{d.market}</td><td>{d.ticker}</td><td>{d.action}</td>
                <td>{d.target_weight}</td><td>{d.confidence}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div>
          <ErrorBox error={detail.error} />
          {detail.data ? (
            <>
              <h3>
                {detail.data.decision.ticker} {detail.data.decision.action}
              </h3>
              <p className="muted">{String(detail.data.decision.data["rationale"] ?? "")}</p>
              <table>
                <thead>
                  <tr>
                    <th>계좌</th><th>승인</th><th>수량</th><th>주문 상태</th><th>접수번호</th><th>사유</th>
                  </tr>
                </thead>
                <tbody>
                  {detail.data.outcomes.map((o) => (
                    <tr key={o.account_id}>
                      <td>{o.account_id}</td><td>{o.approved ? "예" : "아니오"}</td>
                      <td>{o.adjusted_qty ?? "-"}</td><td>{o.order_status ?? "-"}</td>
                      <td>{o.order_no ?? "-"}</td><td className="muted">{o.reasons.join(" / ")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          ) : (
            <p className="muted">왼쪽에서 결정을 고르세요</p>
          )}
        </div>
      </div>
    </section>
  );
}
