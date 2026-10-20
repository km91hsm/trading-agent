import { useState } from "react";
import { getJson } from "../api";
import { ErrorBox } from "../components/Tables";
import { useApp, useLoad } from "../hooks";
import { type FeatureRow, type HistoryResp, KEY_COLUMNS, type LatestResp, type SortDir, fmt, sortRows } from "./features";
import { TodayPage } from "./Today";

function FeatureStore() {
  const { tick } = useApp();
  const [market, setMarket] = useState<"KR" | "US">("KR");
  const [sortKey, setSortKey] = useState("ticker");
  const [dir, setDir] = useState<SortDir>("asc");
  const [ticker, setTicker] = useState<string | null>(null);
  const latest = useLoad(() => getJson<LatestResp>(`/api/features/latest?market=${market}`), [market, tick]);
  const history = useLoad(
    () => (ticker ? getJson<HistoryResp>(`/api/features/history?market=${market}&ticker=${encodeURIComponent(ticker)}&days=120`) : null),
    [market, ticker],
  );
  const onSort = (k: string) => {
    if (k === sortKey) setDir(dir === "asc" ? "desc" : "asc");
    else {
      setSortKey(k);
      setDir(k === "ticker" || k === "sector" ? "asc" : "desc");
    }
  };
  const rows: FeatureRow[] = sortRows(latest.data?.rows ?? [], sortKey, dir);
  const histCols = ["date", ...KEY_COLUMNS.filter((c) => c !== "ticker" && c !== "sector")];
  return (
    <section>
      <h2>피처 스토어</h2>
      <p className="muted">
        state/features/&lt;date&gt;.parquet 의 종목별 최신 행. `*_vs_sector` 는 같은 날·같은 섹터(config/sectors.yaml) 중앙값 대비,
        결측(-)은 워밍업·미수집. 헤더를 누르면 정렬, 행을 누르면 아래에 최근 120행.
      </p>
      <div className="row">
        <select value={market} onChange={(e) => { setMarket(e.target.value as "KR" | "US"); setTicker(null); }}>
          <option value="KR">KR</option>
          <option value="US">US</option>
        </select>
        <span className="muted">기준일 {latest.data?.asof ?? "-"} · {rows.length}종목</span>
      </div>
      <ErrorBox error={latest.error} />
      {latest.data && !rows.length && <p className="muted">피처 행 없음 — `trader features backfill` 뒤 다시 보세요</p>}
      {rows.length > 0 && (
        <div style={{ overflowX: "auto" }}>
          <table>
            <thead>
              <tr>
                {KEY_COLUMNS.map((c) => (
                  <th key={c} style={{ cursor: "pointer", whiteSpace: "nowrap" }} onClick={() => onSort(c)}>
                    {c}{sortKey === c ? (dir === "asc" ? " ▲" : " ▼") : ""}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={String(r.ticker)} className={r.ticker === ticker ? "sel" : ""} style={{ cursor: "pointer" }} onClick={() => setTicker(String(r.ticker))}>
                  {KEY_COLUMNS.map((c) => <td key={c}>{fmt(r[c])}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {ticker && (
        <>
          <h3>{market} {ticker} 최근 120행</h3>
          <ErrorBox error={history.error} />
          {history.data && !history.data.rows.length && <p className="muted">행 없음</p>}
          {history.data && history.data.rows.length > 0 && (
            <div style={{ overflowX: "auto", maxHeight: 420, overflowY: "auto" }}>
              <table>
                <thead>
                  <tr>{histCols.map((c) => <th key={c} style={{ whiteSpace: "nowrap" }}>{c}</th>)}</tr>
                </thead>
                <tbody>
                  {[...history.data.rows].reverse().map((r) => (
                    <tr key={String(r.date)}>{histCols.map((c) => <td key={c}>{fmt(r[c])}</td>)}</tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </section>
  );
}

export function DataPage() {
  return (
    <>
      <TodayPage title="데이터 탐색 (00_inputs)" inputsOnly />
      <FeatureStore />
    </>
  );
}
