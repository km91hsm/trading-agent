import { useState } from "react";
import { getJson } from "../api";
import { Badge } from "../components/Badge";
import { EmptyState } from "../components/EmptyState";
import { Section } from "../components/Section";
import { Skeleton } from "../components/Skeleton";
import { Num, Table, Td, Th } from "../components/Table";
import { ErrorBox } from "../components/Tables";
import { Term } from "../components/Term";
import { fmtPct } from "../format";
import { useApp, useLoad } from "../hooks";
import type { Decision, Outcome } from "../types";

type Detail = { decision: Decision & { data: Record<string, unknown> }; outcomes: Outcome[] };

export function ComparePage() {
  const { accounts, tick } = useApp();
  const [sel, setSel] = useState<number | null>(null);
  const list = useLoad(() => getJson<Decision[]>("/api/decisions", { days: "30" }), [tick]);
  const detail = useLoad(() => (sel ? getJson<Detail>(`/api/decisions/${sel}/outcomes`) : null), [sel, tick]);
  const envOf = (id: string) => accounts.find((a) => a.id === id)?.env ?? "paper";
  const d = detail.data?.decision;
  return (
    <>
      <header className="page-h">
        <h1>계좌 비교</h1>
        <p className="sub">같은 결정이 계좌마다 어떻게 판정·주문됐는지 본다 (모의 vs 실전). 결정은 종목당 하루 1회, 판정·주문은 계좌마다.</p>
      </header>
      <div className="cols-2">
        <Section title="결정" aside="최근 30일 · 행을 고르세요">
          <ErrorBox error={list.error} />
          {list.loading && !list.data ? (
            <Skeleton rows={6} cols={5} />
          ) : list.data?.length ? (
            <Table dense caption="최근 결정">
              <thead>
                <tr>
                  <Th>날짜</Th>
                  <Th>종목</Th>
                  <Th>결정</Th>
                  <Th num>
                    <Term k="target_weight">비중</Term>
                  </Th>
                  <Th num>
                    <Term k="confidence" />
                  </Th>
                </tr>
              </thead>
              <tbody>
                {list.data.map((x) => (
                  <tr key={x.id} className={sel === x.id ? "sel clickable" : "clickable"} aria-selected={sel === x.id} onClick={() => setSel(x.id)}>
                    <Td>{x.date}</Td>
                    <Td>
                      <span className="nm">
                        {x.ticker}
                        <small>{x.market}</small>
                      </span>
                    </Td>
                    <Td>
                      <Badge kind="action" value={x.action} />
                    </Td>
                    <Td num>{x.target_weight === null ? "—" : fmtPct(x.target_weight, 0)}</Td>
                    <Td num>{x.confidence === null ? "—" : x.confidence.toFixed(2)}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          ) : (
            <EmptyState title="최근 30일 결정이 없어요" hint="analyze 가 돌면 종목당 하루 한 건씩 쌓여요." />
          )}
        </Section>
        <Section title={d ? `${d.market} ${d.ticker} · ${d.date}` : "계좌별 결과"} aside={d ? <Badge kind="action" value={d.action} /> : undefined}>
          <ErrorBox error={detail.error} />
          {detail.loading && !detail.data ? (
            <Skeleton rows={3} cols={5} />
          ) : d ? (
            <>
              <p className="why-box">{String(d.data["rationale"] ?? "근거 없음")}</p>
              <dl className="kv">
                <dt>
                  <Term k="target_weight" />
                </dt>
                <dd>{d.target_weight === null ? "—" : fmtPct(d.target_weight, 0)}</dd>
                <dt>
                  <Term k="stop_loss" />
                </dt>
                <dd>
                  <Num value={d.data["stop_loss"] as string | null} market={d.market} />
                </dd>
                <dt>
                  <Term k="take_profit" />
                </dt>
                <dd>
                  <Num value={d.data["take_profit"] as string | null} market={d.market} />
                </dd>
              </dl>
              <Table dense caption="계좌별 판정·주문">
                <thead>
                  <tr>
                    <Th>계좌</Th>
                    <Th>판정</Th>
                    <Th num>수량</Th>
                    <Th>주문</Th>
                    <Th>접수번호</Th>
                    <Th>사유</Th>
                  </tr>
                </thead>
                <tbody>
                  {detail.data!.outcomes.map((o) => (
                    <tr key={o.account_id} data-env={envOf(o.account_id)}>
                      <Td>
                        {o.account_id} <Badge kind="env" value={envOf(o.account_id)} />
                      </Td>
                      <Td>{o.approved ? <Badge kind="ok" value="승인" /> : <Badge kind="status" value="rejected" />}</Td>
                      <Td num>{o.adjusted_qty ?? "—"}</Td>
                      <Td>{o.order_status ? <Badge kind="status" value={o.order_status} /> : <span className="muted">없음</span>}</Td>
                      <Td className="muted">{o.order_no ?? "—"}</Td>
                      <Td className="muted">{o.reasons.join(" / ") || "—"}</Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            </>
          ) : (
            <EmptyState title="왼쪽에서 결정을 고르세요" />
          )}
        </Section>
      </div>
    </>
  );
}
