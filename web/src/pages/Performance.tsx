import { useEffect, useRef, useState } from "react";
import { LineSeries, createChart, type IChartApi, type Time } from "lightweight-charts";
import { getJson, withAccount } from "../api";
import { AccountPicker } from "../components/AccountPicker";
import { ErrorBox } from "../components/Tables";
import { useApp, useLoad } from "../hooks";
import type { Market } from "../types";

type Bench = { return: number | null; series: Array<{ time: string; value: number }> };
type Report = {
  account_id: string;
  market: string;
  days: number;
  nav: Array<{ time: string; value: string }>;
  metrics: { return: number | null; mdd: number | null; sortino: number | null; days: number; start: string | null; end: string | null };
  benchmarks: { one_over_n: Bench; buy_and_hold: Bench; quant: Bench | null };
  cost_sensitivity: Record<string, number>;
  turnover: number;
  decision_distribution: Record<string, number>;
  convergence_rate: number | null;
};

const pct = (v: number | null | undefined) => (v === null || v === undefined ? "-" : `${(v * 100).toFixed(2)}%`);

export function PerformancePage() {
  const { accountId, accounts, tick } = useApp();
  const acct = accounts.find((a) => a.id === accountId);
  const [market, setMarket] = useState<Market>("KR");
  const [days, setDays] = useState(30);
  const box = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const rep = useLoad(
    () => (accountId ? getJson<Report>(withAccount(`/api/performance?market=${market}&days=${days}`, accountId)) : null),
    [accountId, market, days, tick],
  );

  useEffect(() => {
    if (!box.current || !rep.data || rep.data.nav.length < 2) {
      // 계좌·시장을 바꿔 곡선을 그릴 수 없게 되면 이전 차트가 남지 않게 지운다
      chartRef.current?.remove();
      chartRef.current = null;
      return;
    }
    const chart = createChart(box.current, { height: 320, layout: { textColor: "#ddd", background: { color: "#161a22" } }, grid: { vertLines: { color: "#222" }, horzLines: { color: "#222" } } });
    chartRef.current = chart;
    const base = parseFloat(rep.data.nav[0].value) || 1;
    const nav = chart.addSeries(LineSeries, { color: "#f5a524", title: "NAV" });
    nav.setData(rep.data.nav.map((p) => ({ time: p.time as Time, value: parseFloat(p.value) / base })));
    const add = (b: Bench | null, color: string, title: string) => {
      if (!b || !b.series.length) return;
      const s = chart.addSeries(LineSeries, { color, title, lineWidth: 1 });
      s.setData(b.series.map((p) => ({ time: p.time as Time, value: p.value })));
    };
    add(rep.data.benchmarks.one_over_n, "#8b5cf6", "1/N");
    add(rep.data.benchmarks.buy_and_hold, "#2f81f7", "B&H");
    add(rep.data.benchmarks.quant, "#22c55e", "quant");
    chart.timeScale().fitContent();
    return () => {
      chart.remove();
      chartRef.current = null;
    };
  }, [rep.data]);

  const m = rep.data?.metrics;
  return (
    <section>
      <h1>성과</h1>
      <div className="row">
        <AccountPicker />
        <label>
          시장
          <select value={market} onChange={(e) => setMarket(e.target.value as Market)}>
            {(acct?.markets ?? ["KR"]).map((x) => (<option key={x}>{x}</option>))}
          </select>
        </label>
        <label>
          기간(일)
          <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
            {[30, 60, 90, 180, 365].map((d) => (<option key={d} value={d}>{d}</option>))}
          </select>
        </label>
      </div>
      <ErrorBox error={rep.error} />
      {m && (
        <div className="cards">
          <div className="card"><h3>수익률</h3><div>{pct(m.return)}</div><div className="muted">{m.start} ~ {m.end} ({m.days}일)</div></div>
          <div className="card"><h3>MDD</h3><div>{pct(m.mdd)}</div></div>
          <div className="card"><h3>Sortino</h3><div>{m.sortino ?? "-"}</div></div>
          <div className="card"><h3>회전율</h3><div>{pct(rep.data?.turnover ?? 0)}</div></div>
          <div className="card"><h3>1/N</h3><div>{pct(rep.data?.benchmarks.one_over_n.return)}</div></div>
          <div className="card"><h3>B&H</h3><div>{pct(rep.data?.benchmarks.buy_and_hold.return)}</div></div>
          <div className="card"><h3>퀀트 단독</h3><div>{pct(rep.data?.benchmarks.quant?.return)}</div></div>
        </div>
      )}
      <div ref={box} className="chart" style={{ minHeight: 320 }} />
      {rep.data && rep.data.nav.length < 2 && <p className="muted">스냅샷이 2일 이상 쌓이면 곡선이 표시됩니다.</p>}
      <h2>비용 민감도 (왕복 bp)</h2>
      <table>
        <thead><tr>{Object.keys(rep.data?.cost_sensitivity ?? {}).map((k) => (<th key={k}>{k}bp</th>))}</tr></thead>
        <tbody><tr>{Object.values(rep.data?.cost_sensitivity ?? {}).map((v, i) => (<td key={i}>{pct(v)}</td>))}</tr></tbody>
      </table>
      <h2>결정 분포 · 수렴율</h2>
      <ul className="inline">
        {Object.entries(rep.data?.decision_distribution ?? {}).map(([k, v]) => (<li key={k}>{k} <b>{v}</b></li>))}
        <li>수렴율 <b>{rep.data?.convergence_rate === null || rep.data?.convergence_rate === undefined ? "-" : pct(rep.data.convergence_rate)}</b></li>
      </ul>
    </section>
  );
}
