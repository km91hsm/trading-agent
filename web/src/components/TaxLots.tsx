// 세금 로트·워시세일 (Phase 25, 표시 전용): FIFO 실현 로트·US 장단기·30일 워시세일 경고 + CSV.
import { getJson, withAccount } from "../api";
import { useApp, useLoad } from "../hooks";
import { lotRows, lotsHeadline, type TaxLotsResp } from "../lots";
import type { Market } from "../types";
import { CsvLink } from "./CsvLink";
import { EmptyState } from "./EmptyState";
import { Section } from "./Section";
import { Table, Td, Th } from "./Table";
import { Term } from "./Term";
import { ErrorBox } from "./Tables";
import { Ticker } from "./Ticker";

export function TaxLotsSection({ accountId, market }: { accountId: string; market: Market }) {
  const { tick } = useApp();
  const res = useLoad(() => (accountId ? getJson<TaxLotsResp>(withAccount(`/api/tax-lots?market=${market}`, accountId)) : null), [accountId, market, tick]);
  const rows = lotRows(res.data);
  return (
    <Section
      title="실현 로트"
      aside={
        <span className="row tight">
          {lotsHeadline(res.data) || "—"}
          {accountId && <CsvLink href={withAccount(`/api/tax-lots?market=${market}`, accountId)} />}
        </span>
      }
    >
      <ErrorBox error={res.error} />
      {rows.length === 0 ? (
        <EmptyState title="실현된 매도가 아직 없어요" hint="체결 동기화로 매수·매도가 쌓이면 FIFO 로 맞춰 보여 줘요." />
      ) : (
        <Table dense responsive caption="실현 로트 (FIFO)">
          <thead>
            <tr>
              <Th core>종목</Th>
              <Th num>수량</Th>
              <Th>보유 기간</Th>
              <Th num core>실현손익</Th>
              <Th>구분</Th>
              <Th>
                <Term k="wash_sale" />
              </Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.key}>
                <Td core>
                  <Ticker market={market} ticker={r.ticker} />
                </Td>
                <Td num>{r.qty}</Td>
                <Td>{r.period}</Td>
                <Td num core className={r.up === null ? "" : r.up ? "up" : "dn"}>
                  {r.pnl}
                </Td>
                <Td>{r.term}</Td>
                <Td className={r.wash ? "pend" : ""}>{r.wash ? "주의" : "—"}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
      <p className="note">{res.data?.note ?? "표시 전용 — 신고용이 아니에요."}</p>
    </Section>
  );
}
