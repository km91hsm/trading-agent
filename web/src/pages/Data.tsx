import { useState } from "react";
import { getJson, postJson } from "../api";
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
import { useToast } from "../components/Toast";
import { RunJobButton, useCanRun } from "../components/RunJob";

const NUMERIC = new Set(KEY_COLUMNS.filter((c) => c !== "ticker" && c !== "sector"));

function FeatureActions({ run, trainAfter, setTrainAfter }: { run: { ok: boolean; reason?: string }; trainAfter: boolean; setTrainAfter: (v: boolean) => void }) {
  return (
    <div className="row">
      <RunJobButton
        spec={{ kind: "features_backfill", params: { years: 3 } }}
        label="3년 백필 실행"
        primary
        disabledReason={run.reason}
        showReason
        confirm={{
          title: "피처 스토어 3년 백필을 실행할까요?",
          body: (
            <>
              <p>유니버스 전체(KR 200 · US 51)의 일봉·재무 피처를 다시 채워요. 수 분에서 수십 분 걸리고 KIS 호출이 많아요. 진행 상황은 대시보드 작업 대기열에 보여요.</p>
              <label className="check">
                <input type="checkbox" checked={trainAfter} onChange={(e) => setTrainAfter(e.target.checked)} /> 끝나면 퀀트 학습도 이어서
              </label>
            </>
          ),
        }}
        extra={() => (trainAfter ? [{ kind: "ml_train", params: {} }] : [])}
      />
      <RunJobButton spec={{ kind: "features_snapshot", params: {} }} label="오늘 스냅샷" disabledReason={run.reason} />
      <RunJobButton spec={{ kind: "features_derive", params: {} }} label="파생 열 재계산" disabledReason={run.reason} />
    </div>
  );
}

function FeatureStore() {
  const { tick, names } = useApp();
  const run = useCanRun();
  const [trainAfter, setTrainAfter] = useState(true);
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
          latest.data && (
            <EmptyState
              title="피처 행이 없어요"
              hint="3년 백필이 유니버스 전체의 일봉·재무 피처를 채워요(수 분~수십 분, KIS 호출 다수). 데몬이 처음 켜질 때는 자동으로 넣어요."
              action={<FeatureActions run={run} trainAfter={trainAfter} setTrainAfter={setTrainAfter} />}
            />
          )
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

type ManualNote = { received_at: string; title: string; url: string; note: string };

/** '읽은 기사 붙여 넣기': 세이브티커처럼 자동 수집이 막힌 사이트에서 사람이 고른 기사를 시장 공통 수동 입력으로 남긴다. */
function ManualInputs() {
  const { token, tick } = useApp();
  const toast = useToast();
  const [market, setMarket] = useState<"KR" | "US">("KR");
  const [title, setTitle] = useState("");
  const [url, setUrl] = useState("");
  const [note, setNote] = useState("");
  const [n, setN] = useState(0);
  const items = useLoad(() => getJson<{ items: ManualNote[] }>(`/api/control/manual-inputs?market=${market}`), [market, tick, n]);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await postJson("/api/control/manual-inputs", { market, title, url, note }, token);
      toast("저장했어요. 다음 분석의 분위기 입력에 들어가요.", "ok");
      setTitle("");
      setUrl("");
      setNote("");
      setN((x) => x + 1);
    } catch (err) {
      toast(`실패: ${(err as Error).message}`, "error");
    }
  };
  return (
    <Section title="읽은 기사 붙여 넣기" aside="시장 공통 수동 입력 · 토큰 필요">
      <p className="note">
        세이브티커처럼 자동 수집이 막힌 사이트에서 읽은 기사를 여기 적어 두면 그 시장의 다음 분석에서 분위기 애널리스트 입력(<code>sentiment.manual_inputs.market_notes</code>)으로 들어가요. 기준 시각 뒤에 적은 항목은 그날 분석에 들어가지 않아요.
      </p>
      <form className="row" onSubmit={submit} aria-label="읽은 기사 붙여 넣기">
        <label className="field">
          <span>시장</span>
          <select value={market} onChange={(e) => setMarket(e.target.value as "KR" | "US")}>
            <option>KR</option>
            <option>US</option>
          </select>
        </label>
        <label className="field">
          <span>제목</span>
          <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="기사 제목" size={28} required />
        </label>
        <label className="field">
          <span>URL</span>
          <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://" size={24} />
        </label>
        <label className="field">
          <span>메모</span>
          <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="왜 중요한지 한 줄" size={28} />
        </label>
        <button type="submit" className="btn btn-primary" disabled={!token || !title.trim()} title={token ? undefined : "토큰이 필요해요 (설정/제어)"}>
          저장
        </button>
      </form>
      {items.data?.items.length ? (
        <Table dense caption="시장 공통 수동 입력">
          <thead>
            <tr>
              <Th>시각(UTC)</Th>
              <Th>제목</Th>
              <Th>메모</Th>
            </tr>
          </thead>
          <tbody>
            {items.data.items.map((it) => (
              <tr key={it.received_at + it.title}>
                <Td className="muted">{it.received_at.slice(0, 16).replace("T", " ")}</Td>
                <Td>{it.url ? <a href={it.url} target="_blank" rel="noreferrer">{it.title}</a> : it.title}</Td>
                <Td className="muted">{it.note || "—"}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
      ) : (
        items.data && <EmptyState title={`${market} 시장 공통 메모가 없어요`} hint="위 폼에 읽은 기사를 적으면 여기 쌓여요." />
      )}
    </Section>
  );
}

type NewsItem = { id: number; source: string; query: string | null; title: string; url: string; published_at: string | null; received_at: string; tickers: string[] };
type NewsResp = { market: string; hours: number; total: number; asof: string; items: NewsItem[] };

/** 시장 전체 뉴스 저장소(원장 news_items): 데몬이 30분마다 질의 묶음으로 모은다. 발굴·시장 세션·종목 세션의 입력. */
function MarketNews() {
  const { tick, names } = useApp();
  const run = useCanRun();
  const [market, setMarket] = useState<"KR" | "US">("KR");
  const news = useLoad(() => getJson<NewsResp>(`/api/news?market=${market}&hours=48&limit=100`), [market, tick]);
  return (
    <Section
      title="시장 뉴스"
      aside={
        <span className="row">
          <label className="field">
            <span>시장</span>
            <select value={market} onChange={(e) => setMarket(e.target.value as "KR" | "US")}>
              <option>KR</option>
              <option>US</option>
            </select>
          </label>
          <RunJobButton spec={{ kind: "news_collect", params: {} }} label="지금 수집" disabledReason={run.reason} />
        </span>
      }
    >
      <p className="note">
        데몬이 질의 묶음(<code>sources.yaml news.market_queries</code>)으로 최근 48시간 뉴스를 모아 두고, 종목 발굴·시장 세션·종목 세션이 이 저장소를 읽어요. 태그는 유니버스 종목명이 제목·요약에 있을 때만 붙어요.
      </p>
      {news.error && <ErrorBox error={news.error} />}
      {news.loading && !news.data && <Skeleton rows={6} />}
      {news.data?.items.length ? (
        <Table dense caption={`${market} 시장 뉴스`}>
          <thead>
            <tr>
              <Th>시각(UTC)</Th>
              <Th>소스</Th>
              <Th>제목</Th>
              <Th>태그 종목</Th>
            </tr>
          </thead>
          <tbody>
            {news.data.items.map((it) => (
              <tr key={it.id}>
                <Td className="muted">{(it.published_at ?? it.received_at).slice(0, 16).replace("T", " ")}</Td>
                <Td className="muted">{it.source}{it.query ? ` · ${it.query}` : ""}</Td>
                <Td>
                  <a href={it.url} target="_blank" rel="noreferrer">{it.title}</a>
                </Td>
                <Td>{it.tickers.length ? it.tickers.map((t) => <span key={t} className="chip">{labelOf(names, market, t)}</span>) : <span className="muted">—</span>}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
      ) : (
        news.data && <EmptyState title={`${market} 시장 뉴스가 없어요`} hint="데몬이 켜져 있으면 30분마다 모여요. '지금 수집'으로 바로 받을 수도 있어요." />
      )}
    </Section>
  );
}

export function DataPage() {
  return (
    <>
      <TodayPage title="데이터" inputsOnly />
      <MarketNews />
      <FeatureStore />
      <ManualInputs />
    </>
  );
}
