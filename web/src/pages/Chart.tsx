import { useEffect, useRef, useState } from "react";
import { CandlestickSeries, LineSeries, createChart, createSeriesMarkers, type IChartApi, type SeriesMarker, type Time } from "lightweight-charts";
import { getJson } from "../api";
import { ErrorBox } from "../components/Tables";
import { useApp, useLoad } from "../hooks";
import type { ChartData, ChartInterval, Market, Marker } from "../types";

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

export type MarkerStyle = { position: "aboveBar" | "belowBar"; color: string; shape: "arrowUp" | "arrowDown" | "circle" | "square" };

/** 주문: 주황 화살표(매도·축소는 위), 결정: 보라 원, 트리거: 청록 사각형(위). */
export function markerStyle(m: Pick<Marker, "kind" | "side">): MarkerStyle {
  if (m.kind === "trigger") return { position: "aboveBar", color: "#2dd4bf", shape: "square" };
  const above = m.side === "SELL" || m.side === "TRIM";
  if (m.kind === "order") return { position: above ? "aboveBar" : "belowBar", color: "#f5a524", shape: above ? "arrowDown" : "arrowUp" };
  return { position: above ? "aboveBar" : "belowBar", color: "#8b5cf6", shape: "circle" };
}

/** 분봉 epoch(초) 을 세션 타임존의 HH:MM 으로. 일봉 문자열은 그대로 (축·크로스헤어 포매터). */
export function formatSessionTime(t: Time | string | number, tz: string): string {
  if (typeof t !== "number") return String(t);
  return new Intl.DateTimeFormat("en-GB", { timeZone: tz, hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(t * 1000));
}

export function ChartPage() {
  const { accounts } = useApp();
  const [ticker, setTicker] = useState("005930");
  const [market, setMarket] = useState<Market>("KR");
  const [exchange, setExchange] = useState("NAS");
  const [accountId, setAccountId] = useState("");
  const [ival, setIval] = useState<ChartInterval>("1d"); // window.setInterval 을 가리지 않게 interval 이 아닌 이름
  const [q, setQ] = useState({ ticker: "005930", market: "KR" as Market, exchange: "NAS", accountId: "", interval: "1d" as ChartInterval });
  const [showSma, setShowSma] = useState(true);
  const box = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const data = useLoad(() => {
    const minute = q.interval === "1m";
    const params: Record<string, string> = { ticker: q.ticker, market: q.market };
    if (minute) params.count = "390";
    else params.days = "160";
    if (q.market === "US") params.exchange = q.exchange;
    if (q.accountId) params.account_id = q.accountId;
    return getJson<ChartData>(minute ? "/api/charts/minute" : "/api/charts", params);
  }, [q]); // 원장 이벤트(tick)마다 KIS 시세를 다시 받지 않는다. 조회 버튼으로만 갱신(분봉은 서버 60초 캐시).

  useEffect(() => {
    if (!box.current || !data.data) return;
    chartRef.current?.remove();
    const minute = data.data.interval === "1m";
    const tz = data.data.tz ?? "Asia/Seoul";
    const fmt = (t: Time) => formatSessionTime(t, tz);
    const chart = createChart(box.current, {
      height: 460,
      layout: { textColor: "#ddd", background: { color: "#161a22" } },
      grid: { vertLines: { color: "#222" }, horzLines: { color: "#222" } },
      timeScale: { timeVisible: minute, secondsVisible: false, ...(minute ? { tickMarkFormatter: fmt } : {}) },
      ...(minute ? { localization: { timeFormatter: fmt } } : {}),
    });
    chartRef.current = chart;
    const series = chart.addSeries(CandlestickSeries, { upColor: "#e5484d", downColor: "#2f81f7", borderVisible: false, wickUpColor: "#e5484d", wickDownColor: "#2f81f7" });
    series.setData(data.data.candles.map((c) => ({ time: c.time as Time, open: +c.open, high: +c.high, low: +c.low, close: +c.close })));
    const markers: SeriesMarker<Time>[] = data.data.markers.map((m) => ({ time: m.time as Time, ...markerStyle(m), text: m.text }));
    createSeriesMarkers(series, markers.sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0)));
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
          setQ({ ticker: ticker.trim(), market, exchange, accountId, interval: ival });
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
          간격
          <select value={ival} onChange={(e) => setIval(e.target.value as ChartInterval)}>
            <option value="1d">일봉</option>
            <option value="1m">1분</option>
          </select>
        </label>
        <label>
          <input type="checkbox" checked={showSma} onChange={(e) => setShowSma(e.target.checked)} /> SMA 20/60
        </label>
        <button type="submit">조회</button>
      </form>
      <ErrorBox error={data.error} />
      <div ref={box} className="chart" />
      <p className="muted">
        ● 결정 마커(보라) ▲▼ 주문 마커(주황) ■ 트리거 마커(청록). 선: SMA20(주황)·SMA60(초록). 시세는 KIS 실전.
        1분봉은 당일(KR 은 KIS 30봉 단위 역방향 페이징, US 는 최근 120봉)이며 주문·트리거 마커만 찍는다.
        {data.data?.interval === "1m" && data.data.session_date ? ` 세션 ${data.data.session_date} (${data.data.tz})` : ""}
      </p>
    </section>
  );
}
