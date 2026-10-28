// 전체 계좌 합산 (Phase 24): 시장(통화) × 모의/실전별 NAV·현금·평가손익·보유·섹터.
// 시장끼리, 모의·실전끼리는 더하지 않는다(PRODUCT.md 원칙 3) — 보유 비중·섹터의 분모도 그 행의 NAV.
import { Fragment } from "react";
import { Link } from "react-router-dom";
import { applyQuotes, liveLabel, type QuotesResp } from "../live";
import { accountSubRows, liveSummary, missingText, overviewRows, type OverviewRow } from "../overview";
import type { Market, Overview } from "../types";
import { Badge } from "./Badge";
import { EmptyState } from "./EmptyState";
import { Section } from "./Section";
import { SectorTable } from "./Sectors";
import { Delta, Table, Td, Th } from "./Table";
import { PositionsTable } from "./Tables";

const ENV_LABEL = { paper: "모의", real: "실전" } as const;

export type LiveQuotes = Partial<Record<Market, QuotesResp | null>>;

/** 한 시장·환경의 보유: 장중이면 실시간 시세로 현재가·평가를 갱신한다(시세는 페이지가 시장별로 한 번 받는다). */
function EnvHoldings({ row, live }: { row: OverviewRow; live: QuotesResp | null | undefined }) {
  const { market, data } = row;
  const applied = applyQuotes(data.positions, live?.quotes, data.nav);
  return (
    <>
      <h3 className="sub-h">
        {market} {ENV_LABEL[row.env]} 보유{live ? <span className="muted"> · {liveLabel(live, applied.liveCount)}</span> : null}
      </h3>
      {data.positions.length ? (
        <PositionsTable rows={applied.positions} nav={applied.liveCount && applied.nav !== null ? String(applied.nav) : data.nav} showAccounts={data.accounts.length > 1} />
      ) : (
        <p className="muted">보유 없음 — 전액 현금이에요. 매수가 체결되면 다음 판정·잔고 새로고침 때 잡혀요.</p>
      )}
    </>
  );
}

function MissingNote({ data }: { data: Overview }) {
  const text = missingText(data);
  if (!text) return null;
  return (
    <p className="note">
      <span className="pend">잔고 미확인 — {text}.</span> 아직 잔고를 한 번도 읽지 않은 계좌예요. <Link to="/account">계좌 상세</Link>에서 계좌를 골라 잔고
      새로고침을 누르면 합산에 들어가요.
    </p>
  );
}

export function OverviewBlock({
  data,
  maxSectorPct,
  accountCount,
  quotes = {},
}: {
  data: Overview | null;
  maxSectorPct: number;
  accountCount: number;
  quotes?: LiveQuotes;
}) {
  if (!data) return null;
  const rows = overviewRows(data);
  if (!data.configured) return <EmptyState title="계좌가 없어요" hint="config/accounts.yaml 에 계좌를 넣으면 여기서 합산해 보여 줘요." />;
  return (
    <Section title="전체 계좌" aside={`${accountCount}개 계좌 · 시장(통화)·모의/실전별 합산`}>
      {rows.length ? (
        <Table dense responsive caption="시장·환경별 합산">
          <thead>
            <tr>
              <Th core>시장·환경</Th>
              <Th num core>순자산</Th>
              <Th num>현금</Th>
              <Th num core>평가손익</Th>
              <Th num>보유</Th>
              <Th>계좌</Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const s = liveSummary(r.data, r.market, quotes[r.market]?.quotes);
              const subs = accountSubRows(r);
              return (
              <Fragment key={`${r.market}:${r.env}`}>
              <tr>
                <Td core>
                  {r.market} <Badge kind="env" value={r.env} />
                  {s.live ? <span className="live-tag">실시간</span> : null}
                </Td>
                <Td num core>
                  <strong>{s.nav}</strong>
                </Td>
                <Td num>{r.cash}</Td>
                <Td num core>
                  {s.pnl} {s.pnlPct !== null ? <Delta value={s.pnlPct * 100} pct /> : null}
                </Td>
                <Td num>{r.holdings}종목</Td>
                <Td>{r.data.accounts.map((a) => a.id).join(", ")}</Td>
              </tr>
              {/* 계좌가 여럿이면 계좌별 순자산·현금(합산 줄 아래, 같은 열) */}
              {subs.map((a) => (
                <tr key={a.id} className="sub-row">
                  <Td core>
                    <span className="muted">└ {a.id}</span>
                  </Td>
                  <Td num core>
                    {a.nav}
                  </Td>
                  <Td num>{a.cash}</Td>
                  <Td num core />
                  <Td num />
                  <Td className="muted">기준 {a.date}</Td>
                </tr>
              ))}
              </Fragment>
              );
            })}
          </tbody>
        </Table>
      ) : (
        <p className="muted">아직 잔고 스냅샷이 있는 계좌가 없어요.</p>
      )}
      <MissingNote data={data} />
      {data.pending_approvals > 0 && (
        <p className="pend">
          승인 대기 {data.pending_approvals}건 — <Link to="/orders">주문/승인</Link>
        </p>
      )}
      {rows.length > 0 && (
        <p className="note">업종 합계가 상한({Math.round(maxSectorPct * 100)}%)을 넘기면 리스크 게이트가 그 업종의 신규·추가 매수를 거부해요(계좌마다 따로 판정). 헤지 ETF·현금은 상한 대상이 아니에요.</p>
      )}
      {rows.map((r) => (
        <div key={`${r.market}:${r.env}`} className="cols-2">
          <div>
            <EnvHoldings row={r} live={quotes[r.market]} />
          </div>
          <div>
            <h3 className="sub-h">
              {r.market} {ENV_LABEL[r.env]} 업종 비중{r.data.unmapped.length ? <span className="muted"> · 업종 미분류 {r.data.unmapped.join(", ")}</span> : null}
            </h3>
            <SectorTable rows={r.data.sectors} market={r.market} maxSectorPct={maxSectorPct} note={false} />
          </div>
        </div>
      ))}
    </Section>
  );
}
