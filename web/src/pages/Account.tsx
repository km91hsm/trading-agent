import { useState } from "react";
import { getJson, postJson, withAccount } from "../api";
import { Badge } from "../components/Badge";
import { EmptyState } from "../components/EmptyState";
import { Glyph } from "../components/Glyph";
import { Section } from "../components/Section";
import { Skeleton } from "../components/Skeleton";
import { SectorTable } from "../components/Sectors";
import { RiskCard } from "../components/RiskCard";
import { asOfInfo, DAY_MS } from "../asof";
import type { RiskResp } from "../riskCard";
import { ApprovalsTable, ErrorBox, OrdersTable, PositionsTable } from "../components/Tables";
import { CsvLink } from "../components/CsvLink";
import { Term } from "../components/Term";
import { useToast } from "../components/Toast";
import { fmtMoney, fmtPct } from "../format";
import { AccountPicker } from "../components/AccountPicker";
import { TaxLotsSection } from "../components/TaxLots";
import { useApp, useEnsureFresh, useLiveQuotes, useLoad } from "../hooks";
import { applyQuotes, liveLabel } from "../live";
import type { Market, PerformanceReport, Portfolio } from "../types";

export function AccountPage() {
  const { accountId, accounts, token, tick, tickOf, connected } = useApp();
  // 전역 tick 대신 이 화면이 읽는 키만 본다(전체 검토 3차). SSE 가 끊긴 동안은 생존 폴링 tick 으로
  const tLive = connected ? 0 : tick;
  // decisions: 보유 표의 손절·익절·목표 비중은 최근 결정에서 붙는다(views.attach_plans, Codex 리뷰)
  const tPortfolio = tickOf("positions", "orders", "approvals", "approvals_pending", "decisions") + tLive;
  // jobs_done: 피처 백필·스냅샷이 끝나면 종가표가 바뀐다(위험 카드·벤치마크, Codex 리뷰)
  const tPerf = tickOf("positions", "orders", "jobs_done") + tLive;
  const tRisk = tickOf("positions", "jobs_done") + tLive;
  const toast = useToast();
  const acct = accounts.find((a) => a.id === accountId);
  const [marketSel, setMarketSel] = useState<Market | null>(null);
  // 계좌가 쓰지 않는 시장을 고른 채 계좌를 바꾸면 그 계좌의 첫 시장으로 (KR 전용 계좌에서 US 조회 400 방지)
  const market: Market = marketSel && acct?.markets.includes(marketSel) ? marketSel : (acct?.markets[0] ?? "KR");
  const pf = useLoad(
    () => (accountId ? getJson<Portfolio>(withAccount(`/api/portfolio?market=${market}`, accountId)) : null),
    [accountId, market, tPortfolio],
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
  const live = useLiveQuotes(market, (snap?.positions ?? []).map((p) => p.ticker));
  useEnsureFresh(accountId ? [market] : []); // 접속 즉시 잔고 신선하게(정규장·2분 넘은 스냅샷만, 조회만)
  const applied = applyQuotes(snap?.positions ?? [], live?.quotes, snap?.nav);
  const perf = useLoad(
    () => (accountId ? getJson<PerformanceReport>(withAccount(`/api/performance?market=${market}&days=30`, accountId)) : null),
    [accountId, market, tPerf],
  );
  const risk = useLoad(
    () => (accountId ? getJson<RiskResp>(withAccount(`/api/portfolio/risk?market=${market}`, accountId)) : null),
    [accountId, market, tRisk],
  );
  // 주말·휴장을 넘겨도 사흘이면 오래된 잔고다(데몬·갱신 멈춤을 드러낸다, Stock D-096)
  const snapAsOf = asOfInfo(snap?.date, new Date(), 3 * DAY_MS, "스냅샷");
  if (!accountId)
    return (
      <>
        <header className="page-h">
          <h1>계좌 상세</h1>
        </header>
        <AccountPicker purpose="보유·주문·업종·성과" />
      </>
    );
  return (
    <>
      <header className="page-h">
        <h1>계좌 상세</h1>
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
      <Section
        title="요약"
        aside={
          snapAsOf ? (
            <span className="row tight">
              {snapAsOf.text}
              {snapAsOf.stale && (
                <span className="badge badge-pending" title="잔고 스냅샷이 오래됐어요 — 데몬·잔고 갱신을 확인하세요">
                  오래됨
                </span>
              )}
            </span>
          ) : (
            "스냅샷 없음"
          )
        }
      >
        {pf.loading && !pf.data ? (
          <Skeleton rows={3} cols={2} />
        ) : snap ? (
          <dl className="kv">
            <dt>
              <Term k="nav" />
            </dt>
            <dd className="num-l">
              <strong className="big">{fmtMoney(applied.liveCount && applied.nav !== null ? applied.nav : snap.nav, market)}</strong>
              {applied.liveCount ? <span className="muted"> 실시간</span> : null}
            </dd>
            <dt>평가손익</dt>
            <dd className={applied.pnl > 0 ? "up" : applied.pnl < 0 ? "dn" : undefined}>
              {snap.positions.length ? fmtMoney(applied.pnl, market) : "—"}
              {applied.liveCount ? <span className="muted"> 실시간</span> : null}
            </dd>
            <dt>현금</dt>
            <dd>{fmtMoney(snap.cash, market)}</dd>
            <dt>
              <Term k="stance" />
            </dt>
            <dd>{stance ? <Term k={`stance.${stance}`} /> : "—"}</dd>
            <dt>미승인</dt>
            <dd>{acct?.pending_approvals ?? 0}건</dd>
            <dt>30일 수익</dt>
            <dd>{perf.data ? `${fmtPct(perf.data.metrics.return)} · MDD ${fmtPct(perf.data.metrics.mdd)}` : "—"}</dd>
          </dl>
        ) : (
          <EmptyState title="아직 스냅샷이 없어요" hint="잔고 새로고침을 누르거나, 분석이 한 번 돌면 판정 때 찍혀요." />
        )}
      </Section>
      <div className="cols-2">
        <Section
          title="보유 종목"
          aside={
            <span className="row tight">
              {`${snap?.positions.length ?? 0}종목`}
              {live && <span className="muted">{liveLabel(live, applied.liveCount)}</span>}
              {accountId && <CsvLink href={withAccount(`/api/portfolio?market=${market}`, accountId)} />}
            </span>
          }
        >
          {pf.loading && !pf.data ? <Skeleton rows={3} cols={7} /> : <PositionsTable rows={applied.positions} nav={applied.liveCount && applied.nav !== null ? String(applied.nav) : snap?.nav} bandPct={pf.data?.rebalance_band_pct ?? 0.05} />}
        </Section>
        <Section title="업종 비중" aside={`상한 ${Math.round((pf.data?.max_sector_pct ?? 0.4) * 100)}% · 헤지·현금 제외`}>
          {pf.loading && !pf.data ? (
            <Skeleton rows={3} cols={5} />
          ) : (
            <SectorTable rows={pf.data?.sectors ?? []} market={market} maxSectorPct={pf.data?.max_sector_pct ?? 0.4} />
          )}
        </Section>
      </div>
      <Section title="위험" aside="VaR·CVaR 95% · 베타 · 위험 기여 · 표시 전용">
        {risk.loading && !risk.data ? <Skeleton rows={3} cols={3} /> : risk.data ? <RiskCard data={risk.data} market={market} /> : null}
      </Section>
      <Section title="승인 요청">
        <ApprovalsTable rows={pf.data?.approvals ?? []} realAccounts={acct?.env === "real" ? [acct.id] : []} />
      </Section>
      <Section title="최근 주문" aside={`${market} · 최신순`}>
        <OrdersTable rows={pf.data?.orders ?? []} />
      </Section>
      <TaxLotsSection accountId={accountId} market={market} />
    </>
  );
}
