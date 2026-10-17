import { useEffect, useRef, useState } from "react";
import { CandlestickSeries, LineSeries, createChart, createSeriesMarkers, type IChartApi, type SeriesMarker, type Time } from "lightweight-charts";
import { getJson } from "../api";
import { ErrorBox } from "../components/Tables";
import { useApp, useLoad } from "../hooks";
import type { ChartData, Market } from "../types";

/** 단순이동평균: 앞 n−1 개는 값 없음(선이 n 번째 봉부터 시작). */
export function sma(closes: { time: Time; close: number }[], n: number): { time: Time; value: number }[] {
  const out: { time: Time; value: number }[] = [];
  let sum = 0;
  for (let i = 0; i < closes.length; i++) {
    sum += closes[i].close;
    if (i >= n) sum -= closes[i - n].close;
    if (i >= n - 1) out.push({ time: closes[i].time, value: sum / n });
  }
  return out;
}

export function ChartPage() {
  const { accounts } = useApp();
  const [ticker, setTicker] = useState("005930");
  const [market, setMarket] = useState<Market>("KR");
  const [exchange, setExchange] = useState("NAS");
  const [accountId, setAccountId] = useState("");
  const [q, setQ] = useState({ ticker: "005930", market: "KR" as Market, exchange: "NAS", accountId: "" });
  const [showSma, setShowSma] = useState(true);
  const box = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const data = useLoad(() => {
    const params: Record<string, string> = { ticker: q.ticker, market: q.market, days: "160" };
    if (q.market === "US") params.exchange = q.exchange;
    if (q.accountId) params.account_id = q.accountId;
    return getJson<ChartData>("/api/charts", params);
  }, [q]); // 원장 이벤트(tick)마다 KIS 일봉을 다시 받지 않는다. 조회 버튼으로만 갱신.

  useEffect(() => {
    if (!box.current || !data.data) return;
    chartRef.current?.remove();
    const chart = createChart(box.current, { height: 460, layout: { textColor: "#ddd", background: { color: "#161a22" } }, grid: { vertLines: { color: "#222" }, horzLines: { color: "#222" } } });
    chartRef.current = chart;
    const series = chart.addSeries(CandlestickSeries, { upColor: "#e5484d", downColor: "#2f81f7", borderVisible: false, wickUpColor: "#e5484d", wickDownColor: "#2f81f7" });
    series.setData(data.data.candles.map((c) => ({ time: c.time as Time, open: +c.open, high: +c.high, low: +c.low, close: +c.close })));
    const markers: SeriesMarker<Time>[] = data.data.markers.map((m) => ({
      time: m.time as Time,
      position: m.side === "SELL" || m.side === "TRIM" ? "aboveBar" : "belowBar",
      color: m.kind === "order" ? "#f5a524" : "#8b5cf6",
      shape: m.kind === "order" ? (m.side === "SELL" ? "arrowDown" : "arrowUp") : "circle",
      text: m.text,
    }));
    createSeriesMarkers(series, markers.sort((a, b) => String(a.time).localeCompare(String(b.time))));
    if (showSma) {
      const closes = data.data.candles.map((c) => ({ time: c.time as Time, close: +c.close }));
      for (const [n, color] of [[20, "#f5a524"], [60, "#2fbf71"]] as const) {
        const line = chart.addSeries(LineSeries, { color, lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
        line.setData(sma(closes, n));
      }
    }
    chart.timeScale().fitContent();
    return () => {
      chart.remove();
      chartRef.current = null;
    };
  }, [data.data, showSma]);

  return (
    <section>
      <h1>차트</h1>
      <form
        className="row"
        onSubmit={(e) => {
          e.preventDefault();
          setQ({ ticker: ticker.trim(), market, exchange, accountId });
        }}
      >
        <label>
          종목 <input value={ticker} onChange={(e) => setTicker(e.target.value)} />
        </label>
        <label>
          시장
          <select value={market} onChange={(e) => setMarket(e.target.value as Market)}>
            <option>KR</option><option>US</option>
          </select>
        </label>
        {market === "US" && (
          <label>
            거래소
            <select value={exchange} onChange={(e) => setExchange(e.target.value)}>
              <option>NAS</option><option>NYS</option><option>AMS</option>
            </select>
          </label>
        )}
        <label>
          주문 마커 계좌
          <select value={accountId} onChange={(e) => setAccountId(e.target.value)}>
            <option value="">전체</option>
            {accounts.map((a) => (
              <option key={a.id} value={a.id}>{a.id}</option>
            ))}
          </select>
        </label>
        <label>
          <input type="checkbox" checked={showSma} onChange={(e) => setShowSma(e.target.checked)} /> SMA 20/60
        </label>
        <button type="submit">조회</button>
      </form>
      <ErrorBox error={data.error} />
      <div ref={box} className="chart" />
      <p className="muted">● 결정 마커(보라) ▲▼ 주문 마커(주황). 선: SMA20(주황)·SMA60(초록). 일봉은 KIS 실전 시세.</p>
    </section>
  );
}
