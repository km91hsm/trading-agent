import { useState } from "react";
import { getJson, postJson, withAccount } from "../api";
import { Badge } from "../components/Badge";
import { EmptyState } from "../components/EmptyState";
import { Glyph } from "../components/Glyph";
import { Section } from "../components/Section";
import { Skeleton } from "../components/Skeleton";
import { SectorTable } from "../components/Sectors";
import { ApprovalsTable, ErrorBox, OrdersTable, PositionsTable } from "../components/Tables";
import { Term } from "../components/Term";
import { useToast } from "../components/Toast";
import { fmtMoney } from "../format";
import { useApp, useLoad } from "../hooks";
import type { Market, Portfolio } from "../types";

export function AccountPage() {
  const { accountId, accounts, token, tick } = useApp();
  const toast = useToast();
  const acct = accounts.find((a) => a.id === accountId);
  const [marketSel, setMarketSel] = useState<Market | null>(null);
  // 계좌가 쓰지 않는 시장을 고른 채 계좌를 바꾸면 그 계좌의 첫 시장으로 (KR 전용 계좌에서 US 조회 400 방지)
  const market: Market = marketSel && acct?.markets.includes(marketSel) ? marketSel : (acct?.markets[0] ?? "KR");
  const pf = useLoad(
    () => (accountId ? getJson<Portfolio>(withAccount(`/api/portfolio?market=${market}`, accountId)) : null),
    [accountId, market, tick],
  );
  const refresh = async () => {
    try {
      await postJson(withAccount(`/api/portfolio/refresh?market=${market}`, accountId), null, token);
      toast("잔고를 새로 읽었어요", "ok");
      pf.reload();
    } catch (e) {
      toast(`새로고침 실패: ${(e as Error).message}`, "error");
    }
  };
  const snap = pf.data?.snapshot ?? null;
  const stance = acct?.stance_by_market[market];
  return (
    <>
      <header className="page-h">
        <h1>계좌</h1>
        <p className="sub">
          {acct ? (
            <>
              {acct.id} <Badge kind="env" value={acct.env} /> <Badge kind="mode" value={acct.mode} />
              {acct.mode !== acct.yaml_mode && <span className="muted"> (yaml 은 {acct.yaml_mode})</span>}
            </>
          ) : (
            "계좌를 고르세요"
          )}
        </p>
      </header>
      <div className="row">
        <label className="field">
          <span>시장</span>
          <select value={market} onChange={(e) => setMarketSel(e.target.value as Market)}>
            {(acct?.markets ?? ["KR"]).map((m) => (
              <option key={m}>{m}</option>
            ))}
          </select>
        </label>
        <button type="button" className="btn" onClick={refresh} disabled={!token} title={token ? "KIS 에서 잔고를 다시 읽는다" : "토큰이 필요합니다"}>
          <Glyph name="refresh" size={14} /> 잔고 새로고침
        </button>
        {!token && <span className="note">새로고침은 설정/제어에서 토큰을 넣어야 해요.</span>}
      </div>
      <ErrorBox error={pf.error} />
      <Section title="요약" aside={snap ? `스냅샷 ${snap.date}` : "스냅샷 없음"}>
        {pf.loading && !pf.data ? (
          <Skeleton rows={3} cols={2} />
        ) : snap ? (
          <dl className="kv">
            <dt>
              <Term k="nav" />
            </dt>
            <dd className="num-l">
              <strong className="big">{fmtMoney(snap.nav, market)}</strong>
            </dd>
            <dt>현금</dt>
            <dd>{fmtMoney(snap.cash, market)}</dd>
            <dt>
              <Term k="stance" />
            </dt>
            <dd>{stance ? <Term k={`stance.${stance}`} /> : "—"}</dd>
            <dt>미승인</dt>
            <dd>{acct?.pending_approvals ?? 0}건</dd>
          </dl>
        ) : (
          <EmptyState title="아직 스냅샷이 없어요" hint="잔고 새로고침을 누르거나, 분석이 한 번 돌면 판정 때 찍혀요." />
        )}
      </Section>
      <div className="cols-2">
        <Section title="보유 종목" aside={`${snap?.positions.length ?? 0}종목`}>
          {pf.loading && !pf.data ? <Skeleton rows={3} cols={7} /> : <PositionsTable rows={snap?.positions ?? []} nav={snap?.nav} />}
        </Section>
        <Section title="섹터 비중" aside={`상한 ${Math.round((pf.data?.max_sector_pct ?? 0.4) * 100)}% · 헤지·현금 제외`}>
          {pf.loading && !pf.data ? (
            <Skeleton rows={3} cols={5} />
          ) : (
            <SectorTable rows={pf.data?.sectors ?? []} market={market} maxSectorPct={pf.data?.max_sector_pct ?? 0.4} />
          )}
        </Section>
      </div>
      <Section title="승인 요청">
        <ApprovalsTable rows={pf.data?.approvals ?? []} realAccounts={acct?.env === "real" ? [acct.id] : []} />
      </Section>
      <Section title="최근 주문" aside={`${market} · 최신순`}>
        <OrdersTable rows={pf.data?.orders ?? []} />
      </Section>
    </>
  );
}
