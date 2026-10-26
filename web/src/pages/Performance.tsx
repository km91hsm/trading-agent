import { useEffect, useRef, useState } from "react";
import { AccountPicker } from "../components/AccountPicker";
import { LineSeries, createChart, type IChartApi, type Time } from "lightweight-charts";
import { getJson, withAccount } from "../api";
import { chartOptions, chartPalette } from "../chart";
import { Badge } from "../components/Badge";
import { EmptyState } from "../components/EmptyState";
import { Section } from "../components/Section";
import { Skeleton } from "../components/Skeleton";
import { Delta, Table, Td, Th } from "../components/Table";
import { ErrorBox } from "../components/Tables";
import { Term } from "../components/Term";
import { fmtPct } from "../format";
import { useApp, useLoad } from "../hooks";
import { shadowRows } from "../shadow";
import type { Market, PerformanceBench, PerformanceReport, ShadowReport } from "../types";

const pctNum = (v: number | null | undefined) => (v === null || v === undefined ? null : v * 100);

export function PerformancePage() {
  const { accountId, accounts, tick, theme } = useApp();
  const acct = accounts.find((a) => a.id === accountId);
  const [marketSel, setMarket] = useState<Market | null>(null);
  // 계좌가 쓰지 않는 시장을 고른 채 계좌를 바꾸면 그 계좌의 첫 시장으로 (KR 을 쓰지 않는 계좌에서 400 방지)
  const market: Market = marketSel && acct?.markets.includes(marketSel) ? marketSel : (acct?.markets[0] ?? "KR");
  const [days, setDays] = useState(30);
  const box = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const rep = useLoad(
    () =>
      accountId
        ? getJson<PerformanceReport>(withAccount(`/api/performance?market=${market}&days=${days}`, accountId))
        : null,
    [accountId, market, days, tick],
  );

  const shadow = useLoad(() => getJson<ShadowReport>(`/api/performance/shadow?market=${market}`), [market, tick]);

  useEffect(() => {
    if (!box.current || !rep.data || rep.data.nav.length < 2) {
      // 계좌·시장을 바꿔 곡선을 그릴 수 없게 되면 이전 차트가 남지 않게 지운다
      chartRef.current?.remove();
      chartRef.current = null;
      return;
    }
    const p = chartPalette(box.current);
    const chart = createChart(box.current, chartOptions(p, 320));
    chartRef.current = chart;
    const base = parseFloat(rep.data.nav[0].value) || 1;
    const nav = chart.addSeries(LineSeries, { color: p.ink, title: "NAV", lineWidth: 2 });
    nav.setData(rep.data.nav.map((pt) => ({ time: pt.time as Time, value: parseFloat(pt.value) / base })));
    const add = (b: PerformanceBench | null, color: string, title: string) => {
      if (!b || !b.series.length) return;
      const s = chart.addSeries(LineSeries, { color, title, lineWidth: 1 });
      s.setData(b.series.map((pt) => ({ time: pt.time as Time, value: pt.value })));
    };
    add(rep.data.benchmarks.one_over_n, p.pending, "1/N");
    add(rep.data.benchmarks.buy_and_hold, p.dn, "B&H");
    add(rep.data.benchmarks.quant, p.ok, "quant");
    chart.timeScale().fitContent();
    return () => {
      chart.remove();
      chartRef.current = null;
    };
  }, [rep.data, theme]);

  const m = rep.data?.metrics;
  const b = rep.data?.benchmarks;
  const dist = Object.entries(rep.data?.decision_distribution ?? {});
  if (!accountId)
    return (
      <>
        <header className="page-h">
          <h1>성과</h1>
        </header>
        <AccountPicker purpose="성과" />
      </>
    );
  return (
    <>
      <header className="page-h">
        <h1>성과</h1>
        <p className="sub">
          <Term k="nav" /> 는 판정 때마다 찍은 스냅샷, 벤치마크는 피처 스토어 종가. 실전 전환 준비도는 `trader readiness`.
        </p>
      </header>
      <div className="row">
        <label className="field">
          <span>시장</span>
          <select value={market} onChange={(e) => setMarket(e.target.value as Market)}>
            {(acct?.markets ?? ["KR"]).map((x) => (
              <option key={x}>{x}</option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>기간(일)</span>
          <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
            {[30, 60, 90, 180, 365].map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </select>
        </label>
        {acct && <Badge kind="env" value={acct.env} />}
      </div>
      <ErrorBox error={rep.error} />
      {rep.loading && !rep.data && <Skeleton rows={5} />}
      {rep.data && (
        <div className="cols-2">
          <Section title="지표" aside={m?.start && m?.end ? `${m.start} ~ ${m.end} · ${m.days}일` : `${days}일`}>
            <Table dense caption="성과 지표">
              <tbody>
                <tr>
                  <Td>수익률</Td>
                  <Td num>
                    <Delta value={pctNum(m?.return)} pct />
                  </Td>
                </tr>
                <tr>
                  <Td>
                    <Term k="mdd" />
                  </Td>
                  <Td num>
                    <Delta value={pctNum(m?.mdd)} pct />
                  </Td>
                </tr>
                <tr>
                  <Td>Sortino</Td>
                  <Td num>{m?.sortino ?? "—"}</Td>
                </tr>
                <tr>
                  <Td>회전율</Td>
                  <Td num>{fmtPct(rep.data.turnover)}</Td>
                </tr>
                <tr>
                  <Td>
                    <Term k="benchmark_1n" />
                  </Td>
                  <Td num>
                    <Delta value={pctNum(b?.one_over_n.return)} pct />
                  </Td>
                </tr>
                <tr>
                  <Td>보유 종목 B&amp;H</Td>
                  <Td num>
                    <Delta value={pctNum(b?.buy_and_hold.return)} pct />
                  </Td>
                </tr>
                <tr>
                  <Td>퀀트 상위 5 단독</Td>
                  <Td num>{b?.quant ? <Delta value={pctNum(b.quant.return)} pct /> : <span className="muted">승격 모델 없음</span>}</Td>
                </tr>
                <tr>
                  <Td>
                    <Term k="convergence" />
                  </Td>
                  <Td num>{fmtPct(rep.data.convergence_rate)}</Td>
                </tr>
              </tbody>
            </Table>
          </Section>
          <Section title="비용 민감도" aside="왕복 비용을 바꿨을 때의 수익률">
            <Table dense caption="비용 민감도">
              <thead>
                <tr>
                  {Object.keys(rep.data.cost_sensitivity).map((k) => (
                    <Th key={k} num>
                      {k}bp
                    </Th>
                  ))}
                </tr>
              </thead>
              <tbody>
                <tr>
                  {Object.values(rep.data.cost_sensitivity).map((v, i) => (
                    <Td key={i} num>
                      <Delta value={pctNum(v)} pct />
                    </Td>
                  ))}
                </tr>
              </tbody>
            </Table>
            <h3 className="sub-h">결정 분포</h3>
            <Table dense caption="결정 분포">
              <tbody>
                {dist.length ? (
                  dist.map(([k, v]) => (
                    <tr key={k}>
                      <Td>
                        <Badge kind="action" value={k} />
                      </Td>
                      <Td num>{v}</Td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <Td colSpan={2} className="muted">
                      기간 안 결정 없음
                    </Td>
                  </tr>
                )}
              </tbody>
            </Table>
          </Section>
        </div>
      )}
      <Section title="NAV 곡선" aside="시작일 = 1 로 정규화 · NAV(먹) · 1/N(황) · B&H(청) · quant(녹)">
        <div ref={box} className="chart chart-sm" />
        {rep.data && rep.data.nav.length < 2 && (
          <EmptyState title="곡선을 그릴 스냅샷이 아직 부족해요" hint="판정이 이틀 이상 쌓이면 표시됩니다." />
        )}
      </Section>
      <Section
        title="LLM 종목 선택 효과"
        aside={`${market} · 같은 날 같은 후보를 다르게 샀다면 · 5거래일 수익 차이 · 만기 ${shadow.data?.min_pairs ?? 20}쌍부터 판정`}
      >
        <ErrorBox error={shadow.error} />
        {shadow.data && (
          <Table dense caption="LLM 선택과 기준 전략의 5거래일 수익 차이">
            <thead>
              <tr>
                <Th>비교 기준</Th>
                <Th num>만기 쌍</Th>
                <Th num>평균 차이</Th>
                <Th num>95% 구간</Th>
                <Th>판정</Th>
              </tr>
            </thead>
            <tbody>
              {shadowRows(shadow.data).map((r) => (
                <tr key={r.key}>
                  <Td>{r.label}</Td>
                  <Td num>{r.n}</Td>
                  <Td num>
                    <Delta value={r.meanPct} pct />
                  </Td>
                  <Td num>
                    {r.lowPct === null || r.highPct === null ? (
                      "—"
                    ) : (
                      <>
                        <Delta value={r.lowPct} pct /> ~ <Delta value={r.highPct} pct />
                      </>
                    )}
                  </Td>
                  <Td className={r.verdict === "부족" || r.verdict === "구분 안 됨" ? "muted" : undefined}>{r.verdict}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
        <p className="muted">
          실전 전환 준비도의 'LLM 알파' 는 같은 후보 균등 대비 95% 구간 하한이 0 보다 커야 통과해요. 계좌 수익률과 달리 시장 전체
          움직임이 빠져 종목 선택만 남습니다.
        </p>
      </Section>
    </>
  );
}
