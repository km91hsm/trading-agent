import { useEffect, useRef, useState } from "react";
import {
  CandlestickSeries,
  LineSeries,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type SeriesMarker,
  type Time,
} from "lightweight-charts";
import { getJson } from "../api";
import { chartOptions, chartPalette, formatSessionTime, markerStyle, sma } from "../chart";
import { Glyph } from "../components/Glyph";
import { Section } from "../components/Section";
import { Skeleton } from "../components/Skeleton";
import { ErrorBox } from "../components/Tables";
import { useApp, useLoad } from "../hooks";
import { labelOf } from "../names";
import type { ChartData, ChartInterval, Market } from "../types";

export function ChartPage() {
  const { accounts, theme, names } = useApp();
  const [ticker, setTicker] = useState("005930");
  const [market, setMarket] = useState<Market>("KR");
  const [exchange, setExchange] = useState("NAS");
  const [accountId, setAccountId] = useState("");
  const [ival, setIval] = useState<ChartInterval>("1d"); // window.setInterval 을 가리지 않게 interval 이 아닌 이름
  const [q, setQ] = useState({
    ticker: "005930",
    market: "KR" as Market,
    exchange: "NAS",
    accountId: "",
    interval: "1d" as ChartInterval,
  });
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
    const p = chartPalette(box.current);
    const minute = data.data.interval === "1m";
    const tz = data.data.tz ?? "Asia/Seoul";
    const fmt = (t: Time) => formatSessionTime(t, tz);
    const chart = createChart(box.current, {
      ...chartOptions(p, 460),
      timeScale: { borderColor: p.rule, timeVisible: minute, secondsVisible: false, ...(minute ? { tickMarkFormatter: fmt } : {}) },
      ...(minute ? { localization: { timeFormatter: fmt } } : {}),
    });
    chartRef.current = chart;
    const series = chart.addSeries(CandlestickSeries, {
      upColor: p.up,
      downColor: p.dn,
      borderVisible: false,
      wickUpColor: p.up,
      wickDownColor: p.dn,
    });
    series.setData(
      data.data.candles.map((c) => ({ time: c.time as Time, open: +c.open, high: +c.high, low: +c.low, close: +c.close })),
    );
    const markers: SeriesMarker<Time>[] = data.data.markers.map((m) => ({
      time: m.time as Time,
      ...markerStyle(m, p),
      text: m.text,
    }));
    createSeriesMarkers(series, markers.sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0)));
    if (showSma) {
      const closes = data.data.candles.map((c) => ({ time: c.time as Time, close: +c.close }));
      for (const [n, color] of [[20, p.pending], [60, p.ink2]] as const) {
        const line = chart.addSeries(LineSeries, { color, lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
        line.setData(sma(closes, n));
      }
    }
    chart.timeScale().fitContent();
    return () => {
      chart.remove();
      chartRef.current = null;
    };
  }, [data.data, showSma, theme]);

  return (
    <>
      <header className="page-h">
        <h1>차트</h1>
        <p className="sub">일봉 160일 또는 당일 1분봉. 결정·주문·트리거 마커는 원장에서, 시세는 KIS 실전 서버에서.</p>
      </header>
      <form
        className="row"
        onSubmit={(e) => {
          e.preventDefault();
          setQ({ ticker: ticker.trim(), market, exchange, accountId, interval: ival });
        }}
      >
        <label className="field">
          <span>종목</span>
          <input type="text" value={ticker} onChange={(e) => setTicker(e.target.value)} size={10} />
        </label>
        <label className="field">
          <span>시장</span>
          <select value={market} onChange={(e) => setMarket(e.target.value as Market)}>
            <option>KR</option>
            <option>US</option>
          </select>
        </label>
        {market === "US" && (
          <label className="field">
            <span>거래소</span>
            <select value={exchange} onChange={(e) => setExchange(e.target.value)}>
              <option>NAS</option>
              <option>NYS</option>
              <option>AMS</option>
            </select>
          </label>
        )}
        <label className="field">
          <span>주문 마커 계좌</span>
          <select value={accountId} onChange={(e) => setAccountId(e.target.value)}>
            <option value="">전체</option>
            {accounts.map((a) => (
              <option key={a.id} value={a.id}>
                {a.id}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>간격</span>
          <select value={ival} onChange={(e) => setIval(e.target.value as ChartInterval)}>
            <option value="1d">일봉</option>
            <option value="1m">1분</option>
          </select>
        </label>
        <label className="row tight">
          <input type="checkbox" checked={showSma} onChange={(e) => setShowSma(e.target.checked)} /> SMA 20/60
        </label>
        <button type="submit" className="btn btn-primary">
          조회
        </button>
      </form>
      <Section
        title={`${q.market} ${labelOf(names, q.market, q.ticker)}`}
        aside={
          data.data?.interval === "1m" && data.data.session_date
            ? `세션 ${data.data.session_date} (${data.data.tz})`
            : "일봉 160일"
        }
      >
        <ErrorBox error={data.error} />
        {data.loading && !data.data && <Skeleton rows={6} />}
        <div ref={box} className={data.data ? "chart" : "chart chart-hidden"} />
        <ul className="legend">
          <li>
            <Glyph name="pick" size={10} /> 결정(청)
          </li>
          <li>
            <Glyph name="up" size={10} /> 매수 주문 · <Glyph name="down" size={10} /> 매도 주문(먹)
          </li>
          <li>
            <span className="legend-sq" /> 트리거(황)
          </li>
          <li>SMA20(황) · SMA60(회)</li>
        </ul>
        <p className="note">
          1분봉은 당일(KR 은 KIS 30봉 단위 역방향 페이징, US 는 최근 120봉)이며 주문·트리거 마커만 찍는다.
        </p>
      </Section>
    </>
  );
}
