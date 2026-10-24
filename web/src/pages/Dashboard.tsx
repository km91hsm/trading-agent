import { useEffect, useMemo, useState } from "react";
import { getJson, withAccount } from "../api";
import { Badge } from "../components/Badge";
import { EmptyState } from "../components/EmptyState";
import { Section } from "../components/Section";
import { Skeleton } from "../components/Skeleton";
import { Delta, Mark, Num, Table, Td, Th } from "../components/Table";
import { ErrorBox } from "../components/Tables";
import { Term } from "../components/Term";
import { UsageBlock } from "../components/Usage";
import { JobsBlock } from "../components/Jobs";
import { JobRequestsBlock } from "../components/JobRequests";
import { fmtDate, fmtMoney, fmtPct, fmtTime, minutesLeft } from "../format";
import { useApp, useLoad } from "../hooks";
import { labelOf } from "../names";
import type { UsageResp } from "../usage";
import { daemonText, type DaemonStatus } from "../jobs";
import { proposalText, regimeText, type RegimeResp } from "../sectors";
import { buildTape, gateFor, marketPhase, type TapeRow } from "../tape";
import type {
  Approval,
  CallsResp,
  Checkpoint,
  Decision,
  Order,
  PerformanceReport,
  Portfolio,
  RiskCheck,
  Run,
  SystemSettings,
} from "../types";
import { Ticker } from "../components/Ticker";

type ModelsResp = { active: { version: string; metrics: { alpha: number | null; accuracy: number | null } } | null };
type RlResp = { active: { version: string } | null; experiments: Array<{ version: string }> };
/** 행마다 체크포인트에서 읽는 보조 정보 (30_decision 의 손절·익절·근거, 00_inputs 의 종목명). */
type RowDetail = { name: string | null; stop_loss: string | null; take_profit: string | null; rationale: string | null };

/** 시장별 오늘(운영일): KR 은 KST 날짜, US 는 뉴욕 날짜. */
function runDates(now: Date): Record<string, string> {
  const f = (tz: string) =>
    new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" }).format(now);
  return { KR: f("Asia/Seoul"), US: f("America/New_York") };
}

const str = (v: unknown): string | null => (v === null || v === undefined || v === "" ? null : String(v));

export function Dashboard() {
  const { accounts, accountId, events, tick, names } = useApp();
  const acct = accounts.find((a) => a.id === accountId);
  const markets = acct?.markets ?? [];
  const otherAccounts = accounts.filter((a) => a.id !== accountId);
  const [now, setNow] = useState(() => new Date());
  useEffect(() => setNow(new Date()), [tick]); // 원장 이벤트마다 시각·운영일을 다시 잡는다
  const [minute, setMinute] = useState(0); // 원장과 무관한 것(데몬·사용량)은 60초마다 다시 읽는다
  useEffect(() => {
    const id = setInterval(() => {
      setMinute((m) => m + 1);
      setNow(new Date());
    }, 60_000);
    return () => clearInterval(id);
  }, []);
  const dates = useMemo(() => runDates(now), [now]);
  const [sel, setSel] = useState<string | null>(null);

  const pfKR = useLoad(
    () => (accountId && markets.includes("KR") ? getJson<Portfolio>(withAccount("/api/portfolio?market=KR", accountId)) : null),
    [accountId, markets.join(), tick],
  );
  const pfUS = useLoad(
    () => (accountId && markets.includes("US") ? getJson<Portfolio>(withAccount("/api/portfolio?market=US", accountId)) : null),
    [accountId, markets.join(), tick],
  );
  const decisions = useLoad(() => getJson<Decision[]>("/api/decisions", { days: "2" }), [tick]);
  const checks = useLoad(() => (accountId ? getJson<RiskCheck[]>(withAccount("/api/risk-checks", accountId)) : null), [accountId, tick]);
  const orders = useLoad(() => (accountId ? getJson<Order[]>(withAccount("/api/orders", accountId)) : null), [accountId, tick]);
  const approvals = useLoad(() => (accountId ? getJson<Approval[]>(withAccount("/api/approvals", accountId)) : null), [accountId, tick]);
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
    [otherAccounts.map((a) => a.id).join(), tick],
  );
  const watch = useLoad(() => getJson<{ tickers: string[] }>("/api/control/watchlist"), [tick]);
  const calls = useLoad(() => getJson<CallsResp>("/api/calls", { days: "1" }), [tick]);
  const runs = useLoad(() => getJson<Run[]>("/api/runs", { limit: "30" }), [tick]);
  const settings = useLoad(() => getJson<SystemSettings>("/api/system/settings"), []);
  const usage = useLoad(() => getJson<UsageResp>("/api/system/usage"), [tick, minute]);
  const daemon = useLoad(() => getJson<DaemonStatus>("/api/system/daemon"), [tick, minute]);
  const regime = useLoad(() => getJson<RegimeResp>("/api/system/regime"), [tick]);
  const models = useLoad(() => getJson<ModelsResp>("/api/ml/models"), [tick]);
  const rl = useLoad(() => getJson<RlResp>("/api/rl/experiments"), [tick]);
  const perfKR = useLoad(
    () => (accountId && markets.includes("KR") ? getJson<PerformanceReport>(withAccount("/api/performance?market=KR&days=30", accountId)) : null),
    [accountId, markets.join(), tick],
  );
  const perfUS = useLoad(
    () => (accountId && markets.includes("US") ? getJson<PerformanceReport>(withAccount("/api/performance?market=US&days=30", accountId)) : null),
    [accountId, markets.join(), tick],
  );

  const positions = [...(pfKR.data?.snapshot?.positions ?? []), ...(pfUS.data?.snapshot?.positions ?? [])];
  const tape = useMemo(
    () =>
      buildTape({
        positions,
        decisions: decisions.data ?? [],
        riskChecks: checks.data ?? [],
        orders: orders.data ?? [],
        approvals: approvals.data ?? [],
        watch: watch.data?.tickers ?? [],
        dates,
        nav: { KR: pfKR.data?.snapshot?.nav, US: pfUS.data?.snapshot?.nav },
        now,
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [pfKR.data, pfUS.data, decisions.data, checks.data, orders.data, approvals.data, watch.data, dates, now],
  );
  // 행별 보조 정보: 결정이 있는 행만, 30_decision + 00_inputs 두 파일 (로컬 서버라 N 요청이 부담 없다)
  const decidedKeys = tape.filter((r) => r.decisionId).map((r) => r.key).join();
  const details = useLoad(
    () =>
      decidedKeys
        ? Promise.all(
            tape
              .filter((r) => r.decisionId)
              .map(async (r) => {
                const q = { ticker: r.ticker, market: r.market, date: dates[r.market] };
                const [dec, inp] = await Promise.all([
                  getJson<Checkpoint>("/api/checkpoint", { ...q, stage: "30_decision" }).catch(() => null),
                  getJson<Checkpoint>("/api/checkpoint", { ...q, stage: "00_inputs" }).catch(() => null),
                ]);
                const d = (dec?.data ?? {}) as Record<string, unknown>;
                const meta = ((inp?.data as Record<string, unknown> | undefined)?.meta ?? {}) as Record<string, unknown>;
                const detail: RowDetail = {
                  name: str(meta.name),
                  stop_loss: str(d.stop_loss),
                  take_profit: str(d.take_profit),
                  rationale: str(d.rationale),
                };
                return [r.key, detail] as const;
              }),
          ).then((pairs) => Object.fromEntries(pairs) as Record<string, RowDetail>)
        : null,
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [decidedKeys, dates.KR, dates.US],
  );
  const detailOf = (r: TapeRow): RowDetail | undefined => details.data?.[r.key];
  const otherGate = (r: TapeRow, acctId: string) => {
    if (!r.decisionId) return null;
    const d = (decisions.data ?? []).find((x) => x.id === r.decisionId);
    const rc = (otherChecks.data?.[acctId] ?? []).filter((x) => x.decision_id === r.decisionId).sort((a, b) => b.id - a.id)[0];
    return d ? gateFor(d, rc, undefined, undefined, now) : null;
  };
  const selRow = tape.find((r) => r.key === sel) ?? null;

  const todayOrders = (orders.data ?? []).filter((o) => fmtDate(o.created_at) === dates.KR || fmtDate(o.created_at) === dates.US);
  const pendingApprovals = (approvals.data ?? []).filter((a) => a.status === "pending");
  const runsToday = (runs.data ?? []).filter((r) => fmtDate(r.started_at) === dates.KR);
  const errorsToday = runsToday.filter((r) => r.status === "error");
  const signal = daemonText(daemon.data);
  const regimeChip = regimeText(regime.data);
  const cap = settings.data?.llm?.daily_session_cap;
  const loading = (pfKR.loading || pfUS.loading || decisions.loading) && !tape.length;
  const anyError = pfKR.error || pfUS.error || decisions.error || checks.error || orders.error || approvals.error;
  const phase = marketPhase(now);
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
        <p className="sub">
          {fmtDate(now.toISOString())} · {phase.label} · {fmtTime(now.toISOString(), "KR")} KST
        </p>
      </header>

      <div className="statusline" role="status">
        <span className={signal.level === "none" ? "" : signal.level} title="daemon.lock 의 pid 생존 + 하트비트 파일(잡마다 갱신)">
          데몬 <b>{signal.text}</b>
          {daemon.data?.pid ? <span className="muted"> pid {daemon.data.pid}</span> : null}
        </span>
        <span>
          오늘 세션 <b>{calls.data?.sessions_today ?? 0}{cap ? ` / ${cap}` : ""}</b> · ${(calls.data?.cost_today ?? 0).toFixed(2)}
        </span>
        <span title="구독 사용률 마지막 관측값 (대화형 claude 상태줄)">
          Claude{" "}
          <b>
            {usage.data?.rate_limits ? `5h ${Math.round(usage.data.rate_limits.five_hour?.used_pct ?? 0)}% · 7d ${Math.round(usage.data.rate_limits.seven_day?.used_pct ?? 0)}%` : usage.data ? "관측 없음" : "확인 중"}
          </b>
        </span>
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
      </div>

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
                    <Th>게이트 · {accountId}</Th>
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
                        aria-selected={r.key === sel}
                        onClick={() => setSel(r.key === sel ? null : r.key)}
                      >
                        <Td>
                          <Mark kind={r.mark} />{" "}
                          <Ticker market={r.market} ticker={r.ticker} name={dt?.name} />
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
                  details.loading && !details.data ? (
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
                    <span className="muted">결정 체크포인트 없음</span>
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
                      <Td>{a.order.side}</Td>
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
                      <Td>{o.side}</Td>
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
          <Section title="성과" aside="30일">
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
          <Section title="데몬 잡" aside={daemon.data ? `${daemon.data.jobs.length}개 · 1분마다 갱신` : undefined}>
            <JobsBlock data={daemon.data} now={now} />
          </Section>
          <Section title="작업 대기열" aside="지금 실행 요청 · 데몬이 순서대로">
            <JobRequestsBlock now={now} />
          </Section>
          <Section title="Claude 사용량" aside="구독 창 · 세션">
            <UsageBlock data={usage.data} now={now} />
          </Section>
          <Section title="모델" aside="승격 상태">
            <Table dense caption="모델 승격 상태">
              <tbody>
                <tr>
                  <Td>quant</Td>
                  <Td num>
                    {models.data?.active ? (
                      <>
                        {models.data.active.version} · α {models.data.active.metrics.alpha ?? "—"}
                      </>
                    ) : (
                      <span className="muted">미승격 · ML 없이 운영</span>
                    )}
                  </Td>
                </tr>
                <tr>
                  <Td>RL 사이저</Td>
                  <Td num>{rl.data?.active ? rl.data.active.version : <span className="muted">미승격 · 섀도</span>}</Td>
                </tr>
                <tr>
                  <Td>
                    <Term k="calibration" />
                  </Td>
                  <Td num>
                    <span className="muted">만기 결정 60건부터</span>
                  </Td>
                </tr>
              </tbody>
            </Table>
          </Section>
        </aside>
      </div>
    </>
  );
}
