import { useState } from "react";
import { getJson } from "../api";
import { EmptyState } from "../components/EmptyState";
import { Section } from "../components/Section";
import { Skeleton } from "../components/Skeleton";
import { Table, Td, Th } from "../components/Table";
import { ErrorBox } from "../components/Tables";
import { useApp, useLoad } from "../hooks";
import { labelOf } from "../names";
import { type FeatureRow, type HistoryResp, KEY_COLUMNS, type LatestResp, type SortDir, fmt, sortRows } from "./features";
import { TodayPage } from "./Today";
import { Ticker } from "../components/Ticker";

const NUMERIC = new Set(KEY_COLUMNS.filter((c) => c !== "ticker" && c !== "sector"));

function FeatureStore() {
  const { tick, names } = useApp();
  const [market, setMarket] = useState<"KR" | "US">("KR");
  const [sortKey, setSortKey] = useState("ticker");
  const [dir, setDir] = useState<SortDir>("asc");
  const [ticker, setTicker] = useState<string | null>(null);
  const latest = useLoad(() => getJson<LatestResp>(`/api/features/latest?market=${market}`), [market, tick]);
  const history = useLoad(
    () =>
      ticker
        ? getJson<HistoryResp>(`/api/features/history?market=${market}&ticker=${encodeURIComponent(ticker)}&days=120`)
        : null,
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
    <>
      <Section
        title="피처 스토어"
        aside={
          <>
            기준일 {latest.data?.asof ?? "—"} · {rows.length}종목
          </>
        }
      >
        <p className="note">
          종목별 최신 행. <code>*_vs_sector</code> 는 같은 날·같은 섹터 중앙값 대비, 결측(-)은 워밍업·미수집. 머리를 누르면
          정렬, 행을 누르면 아래에 최근 120행.
        </p>
        <div className="row">
          <label className="field">
            <span>시장</span>
            <select
              value={market}
              onChange={(e) => {
                setMarket(e.target.value as "KR" | "US");
                setTicker(null);
              }}
            >
              <option value="KR">KR</option>
              <option value="US">US</option>
            </select>
          </label>
        </div>
        <ErrorBox error={latest.error} />
        {latest.loading && !latest.data ? (
          <Skeleton rows={6} cols={8} />
        ) : rows.length ? (
          <Table dense caption="피처 스토어 최신 행">
            <thead>
              <tr>
                {KEY_COLUMNS.map((c) => (
                  <Th key={c} num={NUMERIC.has(c)} onSort={() => onSort(c)} sorted={sortKey === c ? dir : null}>
                    {c}
                  </Th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr
                  key={String(r.ticker)}
                  className={r.ticker === ticker ? "sel clickable" : "clickable"}
                  aria-selected={r.ticker === ticker}
                  onClick={() => setTicker(String(r.ticker))}
                >
                  {KEY_COLUMNS.map((c) => (
                    <Td key={c} num={NUMERIC.has(c)}>
                      {c === "ticker" ? <Ticker market={market} ticker={String(r.ticker)} showMarket={false} /> : fmt(r[c])}
                    </Td>
                  ))}
                </tr>
              ))}
            </tbody>
          </Table>
        ) : (
          latest.data && <EmptyState title="피처 행이 없어요" hint="`trader features backfill` 뒤 다시 보세요." />
        )}
      </Section>
      {ticker && (
        <Section title={`${market} ${labelOf(names, market, ticker)} 최근 120행`} aside="최신 날짜가 위">
          <ErrorBox error={history.error} />
          {history.loading && !history.data ? (
            <Skeleton rows={4} cols={8} />
          ) : history.data && history.data.rows.length ? (
            <div className="scroll-box">
              <Table dense caption="피처 이력">
                <thead>
                  <tr>
                    {histCols.map((c) => (
                      <Th key={c} num={c !== "date"}>
                        {c}
                      </Th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {[...history.data.rows].reverse().map((r) => (
                    <tr key={String(r.date)}>
                      {histCols.map((c) => (
                        <Td key={c} num={c !== "date"}>
                          {fmt(r[c])}
                        </Td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </Table>
            </div>
          ) : (
            history.data && <EmptyState title="이력이 없어요" />
          )}
        </Section>
      )}
    </>
  );
}

export function DataPage() {
  return (
    <>
      <TodayPage title="데이터" inputsOnly />
      <FeatureStore />
    </>
  );
}
