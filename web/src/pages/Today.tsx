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
import { type NewsJob, newsJobRows, newsJobsHeadline, pickReason, pipelineStages, scanHeadline, type ScalpResp, scalpChip, scalpRows, stageSummary, type ScanSummary, type Stage } from "../pipeline";
import { Table, Td, Th } from "../components/Table";
import type { Checkpoint, Market } from "../types";
import { Ticker } from "../components/Ticker";
import { RunJobButton, useCanRun } from "../components/RunJob";

type TodayResp = { date: string; market: string; tickers: Record<string, string[]>; scan?: ScanSummary | null; market_files?: string[]; news_jobs?: NewsJob[] };
type MarketCp = { stage: string; schema_version: number | null; data: unknown };

const MARKET_FILE_LABEL: Record<string, string> = {
  _market_inputs: "시장 세션 입력",
  _market_view: "전략가 제안",
  _market_debate_r0_bull: "시장 토론 강세",
  _market_debate_r0_bear: "시장 토론 약세",
  _market_summary: "시장 토론 요약",
  _scan: "스캔 결과",
  _screen: "스크리너 picks",
  _discovery_inputs: "발굴 입력",
  _discovery: "발굴 결과",
};

function marketFileLabel(f: string): string {
  if (MARKET_FILE_LABEL[f]) return MARKET_FILE_LABEL[f];
  const m = /^_discovery(_inputs)?_r(\d+)$/.exec(f);
  if (m) return `장중 발굴 ${m[2]}회차${m[1] ? " 입력" : ""}`;
  if (f.startsWith("_market_analyst_")) return `거시 애널리스트 ${f.slice("_market_analyst_".length)}`;
  return f;
}

/** 시장 단위 단계 파일(시장 세션·스캔·발굴) — 종목 판과 달리 시장 디렉터리에 있다. */
function MarketFiles({ files, market, date }: { files: string[]; market: Market; date: string }) {
  const [stage, setStage] = useState("");
  const cp = useLoad(
    () => (stage ? getJson<MarketCp>("/api/market-checkpoint", { market, date, stage }) : null),
    [market, date, stage],
  );
  return (
    <Section title="시장 단계" aside={`${files.length}개 파일`}>
      <div className="row tight">
        {files.map((f) => (
          <button key={f} type="button" className={f === stage ? "btn btn-sm sel" : "btn btn-sm btn-ghost"} onClick={() => setStage(f === stage ? "" : f)} title={f}>
            {marketFileLabel(f)}
          </button>
        ))}
      </div>
      <ErrorBox error={cp.error} />
      {stage && cp.data && (
        <details open>
          <summary>{marketFileLabel(stage)} · {stage}.json</summary>
          <JsonView value={cp.data.data as Record<string, unknown>} depth={1} />
        </details>
      )}
    </Section>
  );
}

/** 장중 뉴스 발굴(Phase 23): 새 뉴스가 낸 재분석·발굴 요청 — 아침 스캔과 별개로 하루 동안 쌓인다. */
function NewsJobsBlock({ jobs, market, onPick }: { jobs: NewsJob[]; market: Market; onPick: (t: string) => void }) {
  const rows = newsJobRows(jobs);
  return (
    <Section title="장중 발굴" aside={newsJobsHeadline(jobs)}>
      <Table>
        <thead>
          <tr>
            <Th>시각</Th>
            <Th>요청</Th>
            <Th>종목</Th>
            <Th>상태</Th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id}>
              <Td className="mono">{r.at.slice(11, 16)}</Td>
              <Td>{r.kindLabel}</Td>
              <Td>
                {r.ticker ? (
                  <button type="button" className="btn btn-sm btn-ghost" onClick={() => onPick(r.ticker as string)}>
                    <Ticker market={market} ticker={r.ticker} showMarket={false} />
                  </button>
                ) : (
                  <span className="muted">—</span>
                )}
              </Td>
              <Td>
                <Badge kind={r.failed ? "pending" : r.pending ? "pending" : "neutral"} value={r.stateLabel} title={r.error ?? undefined} />
                {r.error && <span className="muted"> {r.error}</span>}
              </Td>
            </tr>
          ))}
        </tbody>
      </Table>
    </Section>
  );
}

/** 분 단위 단타(Phase 23 S, 모의 전용): 오늘 진입·청산. 꺼져 있으면 판을 그리지 않는다. */
function ScalpBlock({ scalp, market, onPick }: { scalp: ScalpResp; market: Market; onPick: (t: string) => void }) {
  const rows = scalpRows(scalp, market);
  const chip = scalpChip(scalp, market);
  const halted = scalp.markets[market]?.halted;
  return (
    <Section title="단타 (모의 전용)" aside={chip.text}>
      {halted && <p className="muted">오늘 중단: {halted}</p>}
      {rows.length === 0 ? (
        <p className="muted">오늘 진입 없음 — 후보(picks·새 뉴스·관심종목)의 1분봉에서 거래량 급증 ∧ 당일 고가·VWAP 동시 돌파가 나오면 진입한다.</p>
      ) : (
        <Table>
          <thead>
            <tr>
              <Th>종목</Th>
              <Th>상태</Th>
              <Th>진입</Th>
              <Th>청산</Th>
              <Th num>수익률</Th>
              <Th>사유</Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.key}>
                <Td>
                  <button type="button" className="btn btn-sm btn-ghost" onClick={() => onPick(r.ticker)}>
                    <Ticker market={market} ticker={r.ticker} showMarket={false} />
                  </button>
                  {r.dry && <Badge kind="pending" value="dry-run" title="주문 없이 기록만" />}
                </Td>
                <Td>{r.state}</Td>
                <Td className="mono">{r.entry}</Td>
                <Td className="mono">{r.exit}</Td>
                <Td num className="mono">{r.ret}</Td>
                <Td>{r.reason}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </Section>
  );
}

function ScanBlock({ scan, market, onPick }: { scan: ScanSummary; market: Market; onPick: (t: string) => void }) {
  return (
    <Section title="오늘 스캔" aside={scanHeadline(scan)}>
      {scan.picks.length ? (
        <Table dense caption="스크리너 picks 와 사유">
          <thead>
            <tr>
              <Th>종목</Th>
              <Th>사유</Th>
            </tr>
          </thead>
          <tbody>
            {scan.picks.map((p) => (
              <tr key={p.ticker}>
                <Td>
                  <button type="button" className="btn btn-sm btn-ghost" onClick={() => onPick(p.ticker)}>
                    <Ticker market={market} ticker={p.ticker} showMarket={false} />
                  </button>
                  {p.discovery && <Badge kind="pending" value={p.discovery.tier === "listed" ? "발굴·밖" : "발굴"} title="scout 이 뉴스에서 찾은 종목" />}
                </Td>
                <Td>{pickReason(p)}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
      ) : (
        <p className="muted">picks 없음 — 보유·관심종목만 분석 대상</p>
      )}
    </Section>
  );
}

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
  const run = useCanRun();
  const [analyzeTicker, setAnalyzeTicker] = useState("");
  const [analyzeExchange, setAnalyzeExchange] = useState("NAS");
  const [analyzeDry, setAnalyzeDry] = useState(true);
  const [market, setMarket] = useState<Market>("KR");
  const [date, setDate] = useState("");
  const [ticker, setTicker] = useState("");
  const [stage, setStage] = useState("");
  const today = useLoad(() => getJson<TodayResp>("/api/today", date ? { market, date } : { market }), [market, date, tick]);
  const scalp = useLoad(() => getJson<ScalpResp>("/api/system/scalp", { market }), [market, tick]);
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
      {!inputsOnly && runDate && (today.data?.market_files?.length ?? 0) > 0 && (
        <MarketFiles files={today.data?.market_files ?? []} market={market} date={runDate} />
      )}
      {!inputsOnly && today.data?.scan && (
        <ScanBlock
          scan={today.data.scan}
          market={market}
          onPick={(t) => {
            setTicker(t);
            setStage("");
          }}
        />
      )}
      {!inputsOnly && scalp.data?.enabled && (
        <ScalpBlock
          scalp={scalp.data}
          market={market}
          onPick={(t) => {
            setTicker(t);
            setStage("");
          }}
        />
      )}
      {!inputsOnly && (today.data?.news_jobs?.length ?? 0) > 0 && (
        <NewsJobsBlock
          jobs={today.data?.news_jobs ?? []}
          market={market}
          onPick={(t) => {
            setTicker(t);
            setStage("");
          }}
        />
      )}
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
          <EmptyState
            title="이 날짜에 실행된 종목이 없어요"
            hint="스캔 → 분석을 지금 돌리거나 종목 하나를 바로 분석할 수 있어요. 요청은 데몬이 순서대로 실행하고 결과는 여기에 쌓여요."
            action={
              inputsOnly ? undefined : (
                <div className="row">
                  <RunJobButton
                    spec={{ kind: "scan_analyze", params: { market } }}
                    label={`지금 ${market} 스캔 → 분석`}
                    disabledReason={run.reason}
                    showReason
                    confirm={{
                      title: `${market} 스캔 → 분석을 지금 실행할까요?`,
                      danger: true,
                      body: (
                        <>
                          <p>Claude 세션이 분석 대상 종목 수만큼 뜨고, 데몬이 dry-run 이 아니면 auto 계좌에 주문이 나갈 수 있어요.</p>
                          <p>다른 예약 잡은 끝날 때까지 기다려요.</p>
                        </>
                      ),
                    }}
                  />
                  <form
                    className="row tight"
                    onSubmit={(e) => e.preventDefault()}
                    aria-label="종목 분석 요청"
                  >
                    <label className="field">
                      <span>종목 분석</span>
                      <input value={analyzeTicker} onChange={(e) => setAnalyzeTicker(e.target.value.trim().toUpperCase())} placeholder={market === "KR" ? "005930" : "AAPL"} size={8} />
                    </label>
                    {market === "US" && (
                      <label className="field">
                        <span>거래소</span>
                        <select value={analyzeExchange} onChange={(e) => setAnalyzeExchange(e.target.value)}>
                          <option>NAS</option>
                          <option>NYS</option>
                          <option>AMS</option>
                        </select>
                      </label>
                    )}
                    <label className="check">
                      <input type="checkbox" checked={analyzeDry} onChange={(e) => setAnalyzeDry(e.target.checked)} /> 모의 실행(주문 없음)
                    </label>
                    <RunJobButton
                      spec={() => ({
                        kind: "analyze_ticker",
                        params: { market, ticker: analyzeTicker, dry_run: analyzeDry, ...(market === "US" ? { exchange: analyzeExchange } : {}) },
                      })}
                      label="분석 요청"
                      primary
                      disabledReason={run.reason ?? (analyzeTicker ? undefined : "종목 코드를 입력하세요")}
                      confirm={
                        analyzeDry
                          ? undefined
                          : { title: `${analyzeTicker} 를 실제 모드로 분석할까요?`, danger: true, body: <p>Claude 세션 1회가 뜨고, 결정이 매수·매도면 auto 계좌에 주문이 나갈 수 있어요.</p> }
                      }
                      onDone={() => setAnalyzeTicker("")}
                    />
                  </form>
                </div>
              )
            }
          />
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
