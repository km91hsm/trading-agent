import { useState } from "react";
import { getJson } from "../api";
import { Badge } from "../components/Badge";
import { EmptyState } from "../components/EmptyState";
import { Glyph } from "../components/Glyph";
import { JsonView } from "../components/JsonView";
import { Section } from "../components/Section";
import { Skeleton } from "../components/Skeleton";
import { ErrorBox } from "../components/Tables";
import { Term } from "../components/Term";
import { useApp, useLoad } from "../hooks";
import { Link } from "react-router-dom";
import { labelOf } from "../names";
import { sectionForStage } from "../how";
import { pipelineStages, stageSummary, type Stage } from "../pipeline";
import type { Checkpoint, Market } from "../types";
import { Ticker } from "../components/Ticker";

type TodayResp = { date: string; market: string; tickers: Record<string, string[]> };

function DebateView({ data }: { data: Record<string, unknown> }) {
  const s = (k: string) => String(data[k] ?? "");
  const list = (k: string) => (Array.isArray(data[k]) ? (data[k] as unknown[]) : []);
  const item = (a: unknown, i: number) => <li key={i}>{typeof a === "string" ? a : JSON.stringify(a)}</li>;
  return (
    <div className="debate">
      <p>
        <b>{s("side") === "bull" ? "강세" : s("side") === "bear" ? "약세" : s("side")}</b> 라운드 {s("round")} · 입장{" "}
        <b>{s("stance")}</b> · 밸류에이션 {s("valuation_view")} · 확신 {s("conviction")}
      </p>
      <h3 className="sub-h">논거</h3>
      <ul>{list("arguments").map(item)}</ul>
      {list("rebuttals").length > 0 && (
        <>
          <h3 className="sub-h">반박</h3>
          <ul>{list("rebuttals").map(item)}</ul>
        </>
      )}
      <details>
        <summary>전체 JSON</summary>
        <JsonView value={data} depth={1} />
      </details>
    </div>
  );
}

function StagePlates({
  stages,
  summaries,
  selected,
  onPick,
}: {
  stages: Stage[];
  summaries: Record<string, string>;
  selected: string;
  onPick: (file: string) => void;
}) {
  return (
    <ol className="plates" aria-label="파이프라인 단계">
      {stages.map((st) => (
        <li key={st.key} className={st.done ? "plate done" : "plate"} data-optional={st.optional || undefined}>
          <div className="plate-h">
            {st.done ? <Glyph name="check" size={12} title="완료" /> : <span className="plate-dot" aria-hidden="true" />}
            <Term k={`stage.${st.key}`} />
            {sectionForStage(st.key) && (
              <Link to={`/how#${sectionForStage(st.key)}`} className="plate-how" title="이 단계가 하는 일 (작동 방식)">
                ?
              </Link>
            )}
          </div>
          <p className="plate-sum">{st.done ? summaries[st.key] || "…" : "아직 없음"}</p>
          {st.done && (
            <div className="plate-files">
              {st.files.map((f) => (
                <button
                  key={f}
                  type="button"
                  className={f === selected ? "btn btn-sm sel" : "btn btn-sm btn-ghost"}
                  onClick={() => onPick(f)}
                  title={f}
                >
                  {st.files.length > 1 ? f.replace(/^\d\d_[a-z_]+?_(?=[^_]+$)/, "").replace(/^(bull|bear)$/, (m) => (m === "bull" ? "강세" : "약세")) : "열기"}
                </button>
              ))}
            </div>
          )}
        </li>
      ))}
    </ol>
  );
}

export function TodayPage({ title = "오늘의 실행", inputsOnly = false }: { title?: string; inputsOnly?: boolean }) {
  const { tick, names } = useApp();
  const [market, setMarket] = useState<Market>("KR");
  const [date, setDate] = useState("");
  const [ticker, setTicker] = useState("");
  const [stage, setStage] = useState("");
  const today = useLoad(() => getJson<TodayResp>("/api/today", date ? { market, date } : { market }), [market, date, tick]);
  const files = today.data?.tickers[ticker] ?? [];
  const stages = pipelineStages(files);
  const runDate = today.data?.date;
  // 단계 요약: 선택 종목의 체크포인트를 전부 읽어 한 줄씩 (로컬 서버라 N 요청이 부담 없다)
  const summaries = useLoad(
    () =>
      ticker && runDate && !inputsOnly && files.length
        ? Promise.all(
            files.map((f) =>
              getJson<Checkpoint>("/api/checkpoint", { ticker, market, date: runDate, stage: f })
                .then((cp) => ({ file: f, data: cp.data }))
                .catch(() => ({ file: f, data: null })),
            ),
          ).then((items) => {
            const out: Record<string, string> = {};
            for (const st of pipelineStages(files)) {
              const mine = items.filter((it) => st.files.includes(it.file) && it.data !== null);
              out[st.key] = stageSummary(st.key, mine);
            }
            return out;
          })
        : null,
    [ticker, runDate, market, files.join(), inputsOnly],
  );
  const cp = useLoad(
    () => (ticker && stage && runDate ? getJson<Checkpoint>("/api/checkpoint", { ticker, market, date: runDate, stage }) : null),
    [ticker, stage, market, runDate],
  );
  const cpData = cp.data?.data as Record<string, unknown> | undefined;
  const tickers = Object.keys(today.data?.tickers ?? {});
  const showStages = inputsOnly ? stages.filter((s) => s.key === "00_inputs") : stages;

  return (
    <>
      <header className="page-h">
        <h1>{title}</h1>
        <p className="sub">
          {inputsOnly
            ? "종목별 수집 결과(00_inputs)를 그대로 본다."
            : "종목마다 입력 → 애널리스트 → 토론 → 요약 → 결정 → 게이트 → 주문 순서로 산출물이 쌓인다. 판을 누르면 그 단계의 파일을 연다."}
        </p>
      </header>
      <div className="row">
        <label className="field">
          <span>시장</span>
          <select
            value={market}
            onChange={(e) => {
              setMarket(e.target.value as Market);
              setTicker("");
              setStage("");
            }}
          >
            <option>KR</option>
            <option>US</option>
          </select>
        </label>
        <label className="field">
          <span>날짜</span>
          <input type="date" value={date} onChange={(e) => setDate(e.target.value)} />
        </label>
        <span className="muted">운영일 {runDate ?? "—"}</span>
      </div>
      <ErrorBox error={today.error} />
      <Section title="종목" aside={`${tickers.length}개`}>
        {today.loading && !today.data ? (
          <Skeleton rows={1} cols={4} />
        ) : tickers.length ? (
          <div className="row tight">
            {tickers.map((t) => (
              <button
                key={t}
                type="button"
                className={t === ticker ? "btn sel" : "btn btn-ghost"}
                onClick={() => {
                  setTicker(t);
                  setStage("");
                }}
              >
                <Ticker market={market} ticker={t} showMarket={false} /> <small className="muted">· {today.data?.tickers[t].length}단계</small>
              </button>
            ))}
          </div>
        ) : (
          <EmptyState title="이 날짜에 실행된 종목이 없어요" hint="scan → analyze 가 돌거나 `trader analyze --ticker` 를 실행하면 생겨요." />
        )}
      </Section>
      {ticker && (
        <Section title={`${market} ${labelOf(names, market, ticker)}`} aside={inputsOnly ? undefined : <>단계 {stages.filter((s) => s.done).length} / {stages.length}</>}>
          <StagePlates
            stages={showStages}
            summaries={summaries.data ?? {}}
            selected={stage}
            onPick={(f) => setStage(f === stage ? "" : f)}
          />
        </Section>
      )}
      <ErrorBox error={cp.error} />
      {cp.loading && stage && <Skeleton rows={3} cols={3} />}
      {cp.data && cpData && (
        <Section
          title={stage}
          aside={
            <>
              <Badge kind="neutral" value={`schema v${cp.data.schema_version ?? "?"}`} />
            </>
          }
        >
          <div className="viewer">
            {stage.startsWith("20_debate_") ? <DebateView data={cpData} /> : <JsonView value={cpData} />}
            {cp.data.call ? (
              <details>
                <summary>호출 정보(모델·비용·소요)</summary>
                <JsonView value={cp.data.call} />
              </details>
            ) : null}
          </div>
        </Section>
      )}
    </>
  );
}
