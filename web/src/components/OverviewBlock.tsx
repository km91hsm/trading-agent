// 전체 계좌 합산 (Phase 24): 시장(통화)별 NAV·현금·평가손익·합산 보유·섹터. 시장끼리는 더하지 않는다.
import { Link } from "react-router-dom";
import { overviewRows } from "../overview";
import type { Overview } from "../types";
import { Badge } from "./Badge";
import { Delta, Table, Td, Th } from "./Table";
import { EmptyState } from "./EmptyState";
import { Section } from "./Section";
import { SectorTable } from "./Sectors";
import { PositionsTable } from "./Tables";

export function OverviewBlock({ data, maxSectorPct, accountCount }: { data: Overview | null; maxSectorPct: number; accountCount: number }) {
  const rows = overviewRows(data);
  if (!data) return null;
  if (!rows.length) return <EmptyState title="계좌가 없어요" hint="config/accounts.yaml 에 계좌를 넣으면 여기서 합산해 보여 줘요." />;
  return (
    <Section title="전체 계좌" aside={`${accountCount}개 계좌 · 시장(통화)·모의/실전별 합산`}>
      <Table dense responsive caption="시장별 합산">
        <thead>
          <tr>
            <Th core>시장·환경</Th>
            <Th num core>순자산</Th>
            <Th num>현금</Th>
            <Th num core>평가손익</Th>
            <Th num>보유</Th>
            <Th>스냅샷</Th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={`${r.market}:${r.env}`}>
              <Td core>
                {r.market} <Badge kind="env" value={r.env} />
              </Td>
              <Td num core>
                <strong>{r.nav}</strong>
              </Td>
              <Td num>{r.cash}</Td>
              <Td num core>
                {r.pnl} {r.pnlPct !== null ? <Delta value={r.pnlPct * 100} pct /> : null}
              </Td>
              <Td num>{r.holdings}종목</Td>
              <Td>
                {r.accounts}개 계좌
                {r.missing.length ? <span className="pend"> · 잔고 미확인 {r.missing.join(", ")}</span> : null}
              </Td>
            </tr>
          ))}
        </tbody>
      </Table>
      {rows.some((r) => r.missing.length) && (
        <p className="note">
          잔고 미확인 계좌는 아직 잔고를 한 번도 읽지 않았어요. <Link to="/account">계좌 상세</Link>에서 계좌를 골라 잔고 새로고침을 누르면 합산에 들어가요.
        </p>
      )}
      {data.pending_approvals > 0 && (
        <p className="pend">
          승인 대기 {data.pending_approvals}건 — <Link to="/orders">주문/승인</Link>
        </p>
      )}
      <p className="note">섹터 합계가 상한({Math.round(maxSectorPct * 100)}%)을 넘기면 리스크 게이트가 그 섹터의 신규·추가 매수를 거부해요. 헤지 ETF·현금은 상한 대상이 아니에요.</p>
      {rows.filter((r, i) => rows.findIndex((x) => x.market === r.market) === i).map((r) => (
        <div key={r.market} className="cols-2">
          <div>
            <h3 className="sub-h">{r.market} 보유 · 모든 계좌 (계좌 열로 모의·실전 구분)</h3>
            {r.data.positions.length ? (
              <PositionsTable rows={r.data.positions} nav={r.data.nav} showAccounts />
            ) : (
              <p className="muted">보유 없음 — 전액 현금이에요. 매수가 체결되면 다음 판정·잔고 새로고침 때 잡혀요.</p>
            )}
          </div>
          <div>
            <h3 className="sub-h">
              {r.market} 섹터 비중 · 모든 계좌{r.data.unmapped.length ? <span className="muted"> · 섹터 미분류 {r.data.unmapped.join(", ")}</span> : null}
            </h3>
            <SectorTable rows={r.data.sectors} market={r.market} maxSectorPct={maxSectorPct} note={false} />
          </div>
        </div>
      ))}
    </Section>
  );
}
