import { useMemo, useState } from "react";
import { getJson, withAccount } from "../api";
import { Badge } from "../components/Badge";
import { EmptyState } from "../components/EmptyState";
import { Section } from "../components/Section";
import { Skeleton } from "../components/Skeleton";
import { Delta, Mark, Num, Table, Td, Th } from "../components/Table";
import { ErrorBox } from "../components/Tables";
import { Term } from "../components/Term";
import { fmtDate, fmtMoney, fmtPct, fmtTime, minutesLeft } from "../format";
import { useApp, useLoad } from "../hooks";
import { buildTape, marketPhase } from "../tape";
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

type ModelsResp = { active: { version: string; metrics: { alpha: number | null; accuracy: number | null } } | null };
type RlResp = { active: { version: string } | null; experiments: Array<{ version: string }> };

/** 시장별 오늘(운영일): KR 은 KST 날짜, US 는 뉴욕 날짜. */
function runDates(now: Date): Record<string, string> {
  const f = (tz: string) =>
    new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" }).format(now);
  return { KR: f("Asia/Seoul"), US: f("America/New_York") };
}

export function Dashboard() {
  const { accounts, accountId, events, tick } = useApp();
  const acct = accounts.find((a) => a.id === accountId);
  const markets = acct?.markets ?? [];
  const now = useMemo(() => new Date(), [tick]);
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
  const watch = useLoad(() => getJson<{ tickers: string[] }>("/api/control/watchlist"), [tick]);
  const calls = useLoad(() => getJson<CallsResp>("/api/calls", { days: "1" }), [tick]);
  const runs = useLoad(() => getJson<Run[]>("/api/runs", { limit: "30" }), [tick]);
  const settings = useLoad(() => getJson<SystemSettings>("/api/system/settings"), []);
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
  const selRow = tape.find((r) => r.key === sel) ?? null;
  const selDecision = useLoad(
    () =>
      selRow?.decisionId
        ? getJson<Checkpoint>("/api/checkpoint", {
            ticker: selRow.ticker,
            market: selRow.market,
            date: dates[selRow.market],
            stage: "30_decision",
          })
        : null,
    [selRow?.decisionId],
  );
  const selData = (selDecision.data?.data ?? null) as Record<string, unknown> | null;

  const todayOrders = (orders.data ?? []).filter((o) => fmtDate(o.created_at) === dates.KR || fmtDate(o.created_at) === dates.US);
  const pendingApprovals = (approvals.data ?? []).filter((a) => a.status === "pending");
  const runsToday = (runs.data ?? []).filter((r) => fmtDate(r.started_at) === dates.KR);
  const errorsToday = runsToday.filter((r) => r.status === "error");
  const lastRun = runs.data?.[0];
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
      </tbody>
    );
  };

  return (
    <>
      <header className="page-h">
        <h1>대시보드</h1>
        <p className="sub">
          {fmtDate(now.toISOString())} · {phase.label} · {fmtTime(now.toISOString(), "KR")} KST
          {acct && (
            <>
              {" "}
              · {acct.id} <Badge kind="env" value={acct.env} /> <Badge kind="mode" value={acct.mode} />
            </>
          )}
        </p>
      </header>

      <div className="statusline" role="status">
        <span>
          마지막 잡 <b>{lastRun ? `${lastRun.kind} ${fmtTime(lastRun.started_at, "KR")}` : "없음"}</b>
        </span>
        <span>
          오늘 세션 <b>{calls.data?.sessions_today ?? 0}{cap ? ` / ${cap}` : ""}</b> · ${(calls.data?.cost_today ?? 0).toFixed(2)}
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
                    <Th>게이트 · {accountId}</Th>
                  </tr>
                </thead>
                <tbody>
                  {tape.map((r) => (
                    <tr
                      key={r.key}
                      className={r.key === sel ? "sel clickable" : "clickable"}
                      aria-selected={r.key === sel}
                      onClick={() => setSel(r.key === sel ? null : r.key)}
                    >
                      <Td>
                        <Mark kind={r.mark} />{" "}
                        <span className="nm">
                          {r.ticker}
                          <small>{r.market}</small>
                        </span>
                      </Td>
                      <Td num>
                        <Num value={r.price} market={r.market} />
                      </Td>
                      <Td num>{r.qty || "—"}</Td>
                      <Td num>{r.weight === null ? "—" : fmtPct(r.weight, 1)}</Td>
                      <Td>{r.action ? <Badge kind="action" value={r.action} /> : <span className="muted">—</span>}</Td>
                      <Td num>{r.confidence === null ? "—" : r.confidence.toFixed(2)}</Td>
                      <Td className={r.gate.approved === false ? "gate no" : r.gate.approved ? "gate ok" : "gate muted"}>{r.gate.text}</Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            ) : (
              <EmptyState
                title="오늘 결정도 보유도 아직 없어요"
                hint="scan → analyze 가 돌면 채워져요. 데몬이 떠 있는지, 설정/제어의 일시정지 여부를 확인하세요."
              />
            )}
            {selRow && (
              <div className="why-box">
                {selRow.decisionId ? (
                  selDecision.loading ? (
                    <span className="muted">근거 불러오는 중…</span>
                  ) : selData ? (
                    <>
                      <b>{selRow.ticker}</b> {String(selData.rationale ?? "근거 없음")}
                      <span className="muted">
                        {" "}
                        · <Term k="target_weight" /> {selData.target_weight === undefined ? "—" : fmtPct(Number(selData.target_weight), 0)}
                        {" · "}
                        <Term k="stop_loss" /> {fmtMoney(selData.stop_loss as string | null, selRow.market)}
                        {" · "}
                        <Term k="take_profit" /> {fmtMoney(selData.take_profit as string | null, selRow.market)}
                      </span>
                    </>
                  ) : (
                    <span className="muted">{selDecision.error ?? "결정 체크포인트 없음"}</span>
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
                        <span className="nm">
                          {a.order.ticker}
                          <small>{a.order.market}</small>
                        </span>
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
                        <span className="nm">
                          {o.ticker}
                          <small>{o.market}</small>
                        </span>
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
