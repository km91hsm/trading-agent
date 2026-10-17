import { useState } from "react";
import { getJson } from "../api";
import { JsonView } from "../components/JsonView";
import { ErrorBox } from "../components/Tables";
import { useApp, useLoad } from "../hooks";
import type { Checkpoint, Market } from "../types";

type TodayResp = { date: string; market: string; tickers: Record<string, string[]> };

function DebateView({ data }: { data: Record<string, unknown> }) {
  const s = (k: string) => String(data[k] ?? "");
  const list = (k: string) => (Array.isArray(data[k]) ? (data[k] as unknown[]) : []);
  const item = (a: unknown, i: number) => <li key={i}>{typeof a === "string" ? a : JSON.stringify(a)}</li>;
  return (
    <div className="debate">
      <div>
        <b>{s("side")}</b> 라운드 {s("round")} · 입장 <b>{s("stance")}</b> · 밸류에이션 {s("valuation_view")} · 확신{" "}
        {s("conviction")}
      </div>
      <h4>논거</h4>
      <ul>{list("arguments").map(item)}</ul>
      {list("rebuttals").length > 0 && (
        <>
          <h4>반박</h4>
          <ul>{list("rebuttals").map(item)}</ul>
        </>
      )}
      <JsonView value={data} depth={1} />
    </div>
  );
}

export function TodayPage({ title = "오늘의 실행", inputsOnly = false }: { title?: string; inputsOnly?: boolean }) {
  const { tick } = useApp();
  const [market, setMarket] = useState<Market>("KR");
  const [date, setDate] = useState("");
  const [ticker, setTicker] = useState("");
  const [stage, setStage] = useState("");
  const today = useLoad(
    () => getJson<TodayResp>("/api/today", date ? { market, date } : { market }),
    [market, date, tick],
  );
  const cp = useLoad(
    () =>
      ticker && stage && today.data
        ? getJson<Checkpoint>("/api/checkpoint", { ticker, market, date: today.data.date, stage })
        : null,
    [ticker, stage, market, today.data?.date],
  );
  const stages = (today.data?.tickers[ticker] ?? []).filter((s) => !inputsOnly || s === "00_inputs");
  const cpData = cp.data?.data as Record<string, unknown> | undefined;
  return (
    <section>
      <h1>{title}</h1>
      <div className="row">
        <label>
          시장
          <select
            value={market}
            onChange={(e) => {
              setMarket(e.target.value as Market);
              setTicker("");
              setStage("");
            }}
          >
            <option>KR</option><option>US</option>
          </select>
        </label>
        <label>
          날짜 <input type="date" value={date} onChange={(e) => setDate(e.target.value)} />
        </label>
        <span className="muted">{today.data?.date}</span>
      </div>
      <ErrorBox error={today.error} />
      <div className="row wrap">
        {Object.keys(today.data?.tickers ?? {}).map((t) => (
          <button key={t} className={t === ticker ? "sel" : ""} onClick={() => { setTicker(t); setStage(""); }}>
            {t} <small>{today.data?.tickers[t].length}단계</small>
          </button>
        ))}
        {today.data && !Object.keys(today.data.tickers).length && <p className="muted">이 날짜의 실행 없음</p>}
      </div>
      {ticker && (
        <div className="row wrap">
          {stages.map((s) => (
            <button key={s} className={`chip ${s === stage ? "sel" : ""}`} onClick={() => setStage(s)}>
              {s}
            </button>
          ))}
        </div>
      )}
      <ErrorBox error={cp.error} />
      {cp.data && cpData && (
        <div className="viewer">
          <h3>{cp.data.stage}</h3>
          {stage.startsWith("20_debate_") ? <DebateView data={cpData} /> : <JsonView value={cpData} />}
          {cp.data.call ? (
            <details>
              <summary>호출 정보</summary>
              <JsonView value={cp.data.call} />
            </details>
          ) : null}
        </div>
      )}
    </section>
  );
}
