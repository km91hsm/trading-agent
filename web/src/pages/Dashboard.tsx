import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { getJson, withAccount } from "../api";
import { Badge } from "../components/Badge";
import { EmptyState } from "../components/EmptyState";
import { Section } from "../components/Section";
import { Skeleton } from "../components/Skeleton";
import { Delta, Mark, Num, RowButton, Table, Td, Th } from "../components/Table";
import { ErrorBox } from "../components/Tables";
import { Term } from "../components/Term";
import { fmtDate, fmtMoney, fmtPct, fmtTime, marketDate, minutesLeft } from "../format";
import { useApp, useEnsureFresh, useLiveQuotes, useLoad, useNow } from "../hooks";
import { applyQuotes } from "../live";
import { labelOf } from "../names";
import { usageChip, type UsageResp } from "../usage";
import { daemonText, type DaemonStatus } from "../jobs";
import { proposalText, regimeText, type RegimeResp } from "../sectors";
import { type ScalpResp, scalpChip } from "../pipeline";
import type { OpsResp } from "../ops";
import { Advisory } from "../components/Advisory";
import { OverviewBlock } from "../components/OverviewBlock";
import { ThemeHeatBlock } from "../components/ThemeHeatBlock";
import type { ThemeResp } from "../themeHeat";
import { mergeAccountRows, mergeHoldings, partialText } from "../overview";
import { termLabel } from "../glossary";
import { buildTape, gateFor, rowDetails, type RowDetail, type TapeRow } from "../tape";
import type {
  Approval,
  CallsResp,
  Decision,
  Order,
  Overview,
  PerformanceReport,
  Portfolio,
  RiskCheck,
  Run,
  SystemSettings,
} from "../types";
import { Ticker } from "../components/Ticker";


/** 시장별 오늘(운영일): KR 은 KST 날짜, US 는 뉴욕 날짜. */
function runDates(now: Date): Record<string, string> {
  const f = (tz: string) =>
    new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" }).format(now);
  return { KR: f("Asia/Seoul"), US: f("America/New_York") };
}

export function Dashboard() {
  const { accounts, accountId, events, names, tickOf } = useApp();
  // 바뀐 스냅샷 키에 맞는 표만 다시 읽는다 — 새로고침 없이 실시간(runs 는 판정·세션, positions 는
  // 잔고 스냅샷, usage 는 사용률 관측, control 은 국면·점검·모드 플래그, daemon 은 하트비트)
  const tRuns = tickOf("runs");
  const tPos = tickOf("positions");
  const tUsage = tickOf("usage");
  const tControl = tickOf("control");
  const tDaemon = tickOf("daemon");
  const tTheme = tickOf("theme", "control");
  const tOrders = tickOf("orders");
  const tDecisions = tickOf("decisions");
  const tApprovals = tickOf("approvals", "approvals_pending");
  const tWatch = tickOf("watchlist");
  // '전체' 선택이면 게이트 열의 기준 계좌는 첫 계좌, 보유는 전체 합산(overview)을 쓴다 (Phase 24)
  const primaryId = accountId || accounts[0]?.id || "";
  const acct = accounts.find((a) => a.id === primaryId);
  // 화면에 보일 시장(합산이면 모든 계좌의 합집합)과 대표 계좌가 실제로 가진 시장(계좌 단위 요청은 이것만 — 없는 시장은 400)
  const markets = accountId ? (acct?.markets ?? []) : (["KR", "US"] as const).filter((m) => accounts.some((a) => a.markets.includes(m)));
  const acctMarkets = acct?.markets ?? [];
  const otherAccounts = accounts.filter((a) => a.id !== primaryId);
  // 시각·운영일·남은 분은 30초마다(서버 재조회 없이), 원장 밖 값(데몬·사용량)은 분이 바뀔 때 다시 읽는다
  const now = useNow(30_000);
  const minute = Math.floor(now.getTime() / 60_000);
  const dates = useMemo(() => runDates(now), [now]);
  const [sel, setSel] = useState<string | null>(null);

  const pfKR = useLoad(
    () => (primaryId && acctMarkets.includes("KR") ? getJson<Portfolio>(withAccount("/api/portfolio?market=KR", primaryId)) : null),
    [primaryId, acctMarkets.join(), tRuns, tOrders, tPos],
  );
  const pfUS = useLoad(
    () => (primaryId && acctMarkets.includes("US") ? getJson<Portfolio>(withAccount("/api/portfolio?market=US", primaryId)) : null),
    [primaryId, acctMarkets.join(), tRuns, tOrders, tPos],
  );
  const decisions = useLoad(() => getJson<Decision[]>("/api/decisions", { days: "2" }), [tDecisions]);
  const checks = useLoad(() => (primaryId ? getJson<RiskCheck[]>(withAccount("/api/risk-checks", primaryId)) : null), [primaryId, tRuns, tDecisions]);
  // '전체' 선택이면 모든 계좌의 주문·승인을 합친다(첫 계좌만 세던 버그 — 전체 검토 W3)
  const scopeIds = accountId ? [accountId] : accounts.map((a) => a.id);
  // 실패한 계좌는 빈 목록으로 숨기지 않는다 — 전부 실패면 오류, 일부면 '합계에서 빠짐' 안내
  const orders = useLoad(
    () => (scopeIds.length ? mergeAccountRows(scopeIds, (id) => getJson<Order[]>(withAccount("/api/orders", id))) : null),
    [scopeIds.join(), tOrders],
  );
  const approvals = useLoad(
    () => (scopeIds.length ? mergeAccountRows(scopeIds, (id) => getJson<Approval[]>(withAccount("/api/approvals", id))) : null),
    [scopeIds.join(), tApprovals],
  );
  const orderRows = orders.data?.rows ?? [];
  const approvalRows = approvals.data?.rows ?? [];
  const otherChecks = useLoad(
    () =>
      otherAccounts.length
        ? Promise.all(
            otherAccounts.map((a) =>
              getJson<RiskCheck[]>(withAccount("/api/risk-checks", a.id))
                .then((rows) => [a.id, rows] as const)
                .catch(() => [a.id, [] as RiskCheck[]] as const),
            ),
          ).then((pairs) => Object.fromEntries(pairs) as Record<string, RiskCheck[]>)
        : null,
    [otherAccounts.map((a) => a.id).join(), tRuns, tDecisions],
  );
  const watch = useLoad(() => getJson<{ tickers: string[] }>("/api/control/watchlist"), [tWatch]);
  const calls = useLoad(() => getJson<CallsResp>("/api/calls", { days: "1" }), [tRuns, tUsage]);
  const runs = useLoad(() => getJson<Run[]>("/api/runs", { limit: "30" }), [tRuns]);
  const settings = useLoad(() => getJson<SystemSettings>("/api/system/settings"), [tControl]);
  const usage = useLoad(() => getJson<UsageResp>("/api/system/usage"), [tRuns, tUsage, minute]);
  const daemon = useLoad(() => getJson<DaemonStatus>("/api/system/daemon"), [tDaemon, tRuns, minute]);
  const regime = useLoad(() => getJson<RegimeResp>("/api/system/regime"), [tRuns, tControl]);
  const themeKR = useLoad(() => getJson<ThemeResp>("/api/system/theme", { market: "KR" }), [tRuns, tTheme]);
  const scalp = useLoad(() => getJson<ScalpResp>("/api/system/scalp"), [tRuns, tOrders, minute]);
  const ops = useLoad(() => getJson<OpsResp>("/api/system/ops"), [tRuns, tOrders, tControl]);
  const perfKR = useLoad(
    () => (primaryId && acctMarkets.includes("KR") ? getJson<PerformanceReport>(withAccount("/api/performance?market=KR&days=30", primaryId)) : null),
    [primaryId, acctMarkets.join(), tRuns, tPos],
  );
  const perfUS = useLoad(
    () => (primaryId && acctMarkets.includes("US") ? getJson<PerformanceReport>(withAccount("/api/performance?market=US&days=30", primaryId)) : null),
    [primaryId, acctMarkets.join(), tRuns, tPos],
  );

  const overview = useLoad(() => getJson<Overview>("/api/portfolio/overview"), [tRuns, tOrders, tApprovals, tPos]);
  useEnsureFresh(markets as string[]); // 접속 즉시 잔고 신선하게(정규장·2분 넘은 스냅샷만, 조회만)
  // 시세표의 '보유' 표시: 계좌를 고르면 그 계좌, '전체'면 모든 계좌·환경의 보유 종목(종목별 한 줄)
  const allHeld = (["KR", "US"] as const).flatMap((m) => Object.values(overview.data?.markets[m]?.envs ?? {}).flatMap((e) => e?.positions ?? []));
  const rawPositions = accountId
    ? [...(pfKR.data?.snapshot?.positions ?? []), ...(pfUS.data?.snapshot?.positions ?? [])]
    : mergeHoldings(allHeld);
  // 실시간 시세는 페이지에서 시장별로 한 번(보유 + 결정 종목, ≤20) — 시세표·합산·보유 표가 같은 값을 쓴다
  // 고른 계좌의 보유가 먼저(요청 상한에 걸려도 빠지지 않게), 그다음 전체 보유·오늘 결정 종목
  const liveTickers = (m: string) =>
    Array.from(
      new Set([
        ...rawPositions.filter((p) => (p.market ?? "KR") === m).map((p) => p.ticker),
        ...allHeld.filter((p) => (p.market ?? "KR") === m).map((p) => p.ticker),
        ...(decisions.data ?? []).filter((d) => d.market === m).map((d) => d.ticker),
      ]),
    );
  const liveKR = useLiveQuotes("KR", liveTickers("KR"));
  const liveUS = useLiveQuotes("US", liveTickers("US"));
  const positions = applyQuotes(rawPositions, { ...(liveUS?.quotes ?? {}), ...(liveKR?.quotes ?? {}) }).positions;
  // 시세표 비중의 분모도 실시간 NAV(브로커 총평가 + 평가 변화분) — 평가만 움직이고 NAV 가 그대로면
  // 비중이 100% 를 넘어 보인다. '전체'의 보유 행은 여러 계좌·환경의 합이라 비중을 비운다
  const navOf = (m: "KR" | "US") => {
    if (!accountId) return undefined;
    const snap = (m === "KR" ? pfKR : pfUS).data?.snapshot;
    if (!snap) return undefined;
    const a = applyQuotes(snap.positions ?? [], (m === "KR" ? liveKR : liveUS)?.quotes, snap.nav);
    return a.liveCount && a.nav !== null ? String(a.nav) : snap.nav;
  };

  const tape = useMemo(
    () =>
      buildTape({
        positions,
        decisions: decisions.data ?? [],
        riskChecks: checks.data ?? [],
        // 게이트 열은 대표 계좌 기준이라 그 계좌의 주문·승인만 넘긴다(다른 계좌 실행이 섞이지 않게)
        orders: orderRows.filter((o) => o.account_id === primaryId),
        approvals: approvalRows.filter((a) => a.account_id === primaryId),
        watch: watch.data?.tickers ?? [],
        dates,
        nav: { KR: navOf("KR"), US: navOf("US") },
        now,
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [pfKR.data, pfUS.data, overview.data, accountId, primaryId, decisions.data, checks.data, orders.data, approvals.data, watch.data, dates, now, liveKR, liveUS],
  );
  // 행별 보조 정보(손절·익절·근거)는 결정 목록 응답에 실려 온다 — 체크포인트를 행마다 받지 않는다
  const details = useMemo(() => rowDetails(tape, decisions.data ?? []), [tape, decisions.data]);
  const detailOf = (r: TapeRow): RowDetail | undefined => details[r.key];
  const otherGate = (r: TapeRow, acctId: string) => {
    if (!r.decisionId) return null;
    const d = (decisions.data ?? []).find((x) => x.id === r.decisionId);
    const rc = (otherChecks.data?.[acctId] ?? []).filter((x) => x.decision_id === r.decisionId).sort((a, b) => b.id - a.id)[0];
    return d ? gateFor(d, rc, undefined, undefined, now) : null;
  };
  const selRow = tape.find((r) => r.key === sel) ?? null;

  const todayOrders = orderRows.filter((o) => marketDate(o.created_at, o.market) === dates[o.market]);
  const pendingApprovals = approvalRows.filter((a) => a.status === "pending");
  const runsToday = (runs.data ?? []).filter((r) => fmtDate(r.started_at) === dates.KR);
  const errorsToday = runsToday.filter((r) => r.status === "error");
  const signal = daemonText(daemon.data);
  const regimeChip = regimeText(regime.data);
  const cap = settings.data?.llm?.daily_session_cap;
  const loading = (pfKR.loading || pfUS.loading || decisions.loading) && !tape.length;
  const anyError =
    pfKR.error ||
    pfUS.error ||
    decisions.error ||
    checks.error ||
    orders.error ||
    approvals.error ||
    partialText(orders.data?.failed ?? [], "주문") ||
    partialText(approvals.data?.failed ?? [], "승인 요청");
  const earliestExpiry = pendingApprovals.map((a) => minutesLeft(a.expires_at, now)).filter((m): m is number => m !== null).sort((a, b) => a - b)[0];

  const perfRows = (m: string, rep: PerformanceReport | null) => {
    if (!rep) return null;
    const nav = rep.nav;
    const last = nav[nav.length - 1];
    const prev = nav[nav.length - 2];
    const today = last && prev ? ((parseFloat(last.value) - parseFloat(prev.value)) / parseFloat(prev.value)) * 100 : null;
    const vs1n = rep.metrics.return !== null && rep.benchmarks.one_over_n.return !== null ? (rep.metrics.return - rep.benchmarks.one_over_n.return) * 100 : null;
    return (
      <tbody key={m}>
        <tr>
          <Td>
            <span className="nm">NAV · {m}</span>
          </Td>
          <Td num>
            <strong className="big">{fmtMoney(last?.value, m)}</strong>
          </Td>
        </tr>
        <tr>
          <Td>오늘</Td>
          <Td num>
            <Delta value={today} pct />
          </Td>
        </tr>
        <tr>
          <Td>30일</Td>
          <Td num>
            <Delta value={rep.metrics.return === null ? null : rep.metrics.return * 100} pct />
          </Td>
        </tr>
        <tr>
          <Td>
            <Term k="mdd" />
          </Td>
          <Td num>
            <Delta value={rep.metrics.mdd === null ? null : rep.metrics.mdd * 100} pct />
          </Td>
        </tr>
        <tr>
          <Td>
            <Term k="benchmark_1n">1/N 대비</Term>
          </Td>
          <Td num>{vs1n === null ? "—" : <Delta value={vs1n} pct />}</Td>
        </tr>
        <tr>
          <Td>
            <Term k="convergence" />
          </Td>
          <Td num>{fmtPct(rep.convergence_rate, 0)}</Td>
        </tr>
      </tbody>
    );
  };

  return (
    <>
      <header className="page-h">
        <h1>대시보드</h1>
        <p className="sub">{fmtDate(now.toISOString())} · 새로고침 없이 실시간</p>
      </header>
      <Advisory ops={ops.data} />

      <div className="statusline" role="status">
        <Link to="/system" className={signal.level === "none" ? "" : signal.level} title="daemon.lock 의 pid 생존 + 하트비트 파일(잡마다 갱신) — 누르면 시스템 현황">
          데몬 <b>{signal.text}</b>
          {daemon.data?.pid ? <span className="muted"> pid {daemon.data.pid}</span> : null}
        </Link>
        <span>
          오늘 세션 <b>{calls.data?.sessions_today ?? 0}{cap ? ` / ${cap}` : ""}</b> · ${(calls.data?.cost_today ?? 0).toFixed(2)}
        </span>
        <Link to="/system" title="구독 사용률 최신 관측값 (데몬 확인·분석 세션·대화형 claude 상태줄) — 누르면 시스템 현황">
          Claude{" "}
          <b>
            {usage.data?.rate_limits ? usageChip(usage.data.rate_limits) : usage.data ? "관측 없음" : "확인 중"}
          </b>
        </Link>
        <span className={pendingApprovals.length ? "pend" : ""}>
          미승인 <b>{pendingApprovals.length}건</b>
          {earliestExpiry !== undefined ? ` (${earliestExpiry}분 뒤 만료)` : ""}
        </span>
        <span className={errorsToday.length ? "err" : ""}>
          오늘 오류 <b>{errorsToday.length}건</b>
          {errorsToday[0] ? ` · ${errorsToday[0].kind}` : ""}
        </span>
        <span className={events?.paused ? "err" : ""}>
          <Term k="paused" /> <b>{events?.paused ? "예" : "아니오"}</b>
        </span>
        <span className={regimeChip.level === "none" ? "" : regimeChip.level === "warn" ? "pend" : regimeChip.level} title={regimeChip.title || "지수 proxy 200일선·20일 수익률·변동성 3지표 (매일 scan 앞 판정)"}>
          <Term k="regime" /> <b>{regimeChip.text}</b>
          {regime.data?.enabled && regime.data.markets.some((m) => m.state === "bear") ? <span className="muted"> · 헤지 {Math.round(regime.data.max_weight_pct * 100)}%{proposalText(regime.data) ? ` (${proposalText(regime.data)})` : ""}</span> : null}
        </span>
        {scalp.data?.enabled &&
          markets.map((m) => {
            const chip = scalpChip(scalp.data, m);
            return (
              <span key={m} className={chip.level === "warn" ? "pend" : ""} title="분 단위 단타(모의 전용): 1분봉 거래량 급증 ∧ 당일 고가·VWAP 돌파 → 파이썬이 진입·청산">
                {m} 단타 <b>{chip.text}</b>
              </span>
            );
          })}
      </div>

      <OverviewBlock
        data={overview.data}
        maxSectorPct={Number(settings.data?.risk?.max_sector_pct ?? 0.4)}
        accountCount={accounts.length}
        quotes={{ KR: liveKR, US: liveUS }}
      />
      <ThemeHeatBlock data={themeKR.data} market="KR" />

      <div className="cols">
        <div>
          <Section
            title="오늘의 시세표"
            aside={
              <span className="row tight">
                <Mark kind="held" /> 보유 <Mark kind="pick" /> 선정 <Mark kind="watch" /> 관심
              </span>
            }
          >
            <ErrorBox error={anyError} />
            {loading ? (
              <Skeleton rows={5} cols={7} />
            ) : tape.length ? (
              <Table caption="오늘의 시세표">
                <thead>
                  <tr>
                    <Th>종목</Th>
                    <Th num>현재가</Th>
                    <Th num>보유</Th>
                    <Th num>비중</Th>
                    <Th>결정</Th>
                    <Th num>
                      <Term k="confidence" />
                    </Th>
                    <Th num>
                      <Term k="stop_loss" />
                    </Th>
                    <Th>게이트 · {primaryId}</Th>
                    {otherAccounts.map((a) => (
                      <Th key={a.id}>
                        {a.id} <Badge kind="env" value={a.env} />
                      </Th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {tape.map((r) => {
                    const dt = detailOf(r);
                    return (
                      <tr
                        key={r.key}
                        className={r.key === sel ? "sel clickable" : "clickable"}
                        onClick={() => setSel(r.key === sel ? null : r.key)}
                      >
                        <Td>
                          <RowButton expanded={r.key === sel} onClick={() => setSel(r.key === sel ? null : r.key)}>
                            <Mark kind={r.mark} />{" "}
                            <Ticker market={r.market} ticker={r.ticker} />
                          </RowButton>
                        </Td>
                        <Td num>
                          <Num value={r.price} market={r.market} />
                        </Td>
                        <Td num>{r.qty || "—"}</Td>
                        <Td num>{r.weight === null ? "—" : fmtPct(r.weight, 1)}</Td>
                        <Td>{r.action ? <Badge kind="action" value={r.action} /> : <span className="muted">—</span>}</Td>
                        <Td num>{r.confidence === null ? "—" : r.confidence.toFixed(2)}</Td>
                        <Td num>{dt?.stop_loss ? <Num value={dt.stop_loss} market={r.market} /> : "—"}</Td>
                        <Td className={r.gate.approved === false ? "gate no" : r.gate.approved ? "gate ok" : "gate muted"}>{r.gate.text}</Td>
                        {otherAccounts.map((a) => {
                          const g = otherGate(r, a.id);
                          return (
                            <Td key={a.id} className={g?.approved === false ? "gate no" : g?.approved ? "gate ok" : "gate muted"}>
                              {g ? g.text : "—"}
                            </Td>
                          );
                        })}
                      </tr>
                    );
                  })}
                </tbody>
              </Table>
            ) : (
              <EmptyState
                title="오늘 결정도 보유도 아직 없어요"
                hint="scan → analyze 가 돌면 채워져요. 위 '데몬 신호' 가 오래됐거나 일시정지면 먼저 그쪽을 보세요."
              />
            )}
            {selRow && (
              <div className="why-box">
                {selRow.decisionId ? (
                  decisions.loading && !decisions.data ? (
                    <span className="muted">근거 불러오는 중…</span>
                  ) : detailOf(selRow) ? (
                    <>
                      <b>{labelOf(names, selRow.market, selRow.ticker)}</b> {detailOf(selRow)?.rationale ?? "근거 없음"}
                      <span className="muted">
                        {" · "}
                        <Term k="take_profit" /> {fmtMoney(detailOf(selRow)?.take_profit, selRow.market)}
                      </span>
                    </>
                  ) : (
                    <span className="muted">결정 상세 없음</span>
                  )
                ) : (
                  <span className="muted">오늘 이 종목의 결정이 없어요. 보유분은 monitor 가 손절선·트리거만 봅니다.</span>
                )}
              </div>
            )}
          </Section>

          <Section title="오늘의 주문·승인" aside={`${todayOrders.length + pendingApprovals.length}건 · 시각순`}>
            {todayOrders.length || pendingApprovals.length ? (
              <Table dense caption="오늘의 주문과 승인 요청">
                <thead>
                  <tr>
                    <Th>시각</Th>
                    <Th>종목</Th>
                    <Th>방향</Th>
                    <Th num>수량</Th>
                    <Th num>가격</Th>
                    <Th>상태</Th>
                  </tr>
                </thead>
                <tbody>
                  {pendingApprovals.map((a) => (
                    <tr key={`a${a.id}`}>
                      <Td>{fmtTime(a.requested_at, a.order.market ?? "KR")}</Td>
                      <Td>
                        <Ticker market={a.order.market ?? "KR"} ticker={a.order.ticker ?? "—"} />
                      </Td>
                      <Td>
                        {termLabel(`side.${a.order.side}`)}
                        {!accountId && <span className="muted"> · {a.account_id}</span>}
                      </Td>
                      <Td num>{a.order.qty}</Td>
                      <Td num>
                        <Num value={a.order.price} market={a.order.market ?? "KR"} />
                      </Td>
                      <Td>
                        <Badge kind="status" value="pending" /> <span className="muted">{minutesLeft(a.expires_at, now)}분 뒤 만료</span>
                      </Td>
                    </tr>
                  ))}
                  {todayOrders.map((o) => (
                    <tr key={o.id}>
                      <Td>{fmtTime(o.created_at, o.market)}</Td>
                      <Td>
                        <Ticker market={o.market} ticker={o.ticker} />
                      </Td>
                      <Td>
                        {termLabel(`side.${o.side}`)}
                        {!accountId && <span className="muted"> · {o.account_id}</span>}
                      </Td>
                      <Td num>{o.qty}</Td>
                      <Td num>{o.price ? <Num value={o.price} market={o.market} /> : "시장가"}</Td>
                      <Td>
                        <Badge kind="status" value={o.status} />
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            ) : (
              <EmptyState title="오늘 나간 주문이 없어요" hint="결정이 매수·매도로 나고 리스크 게이트를 통과하면 여기에 쌓여요." />
            )}
          </Section>
        </div>

        <aside>
          <Section title="성과" aside={`30일 · ${primaryId}`}>
            {perfKR.loading && perfUS.loading ? (
              <Skeleton rows={4} cols={2} />
            ) : perfKR.data || perfUS.data ? (
              <Table dense caption="30일 성과">
                {perfRows("KR", perfKR.data)}
                {perfRows("US", perfUS.data)}
              </Table>
            ) : (
              <EmptyState title="성과를 낼 스냅샷이 없어요" hint="판정이 쌓이면 NAV 와 벤치마크가 표시돼요." />
            )}
          </Section>
        </aside>
      </div>
    </>
  );
}
