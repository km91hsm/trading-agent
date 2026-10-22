import { useState } from "react";
import { getJson, withAccount } from "../api";
import { Badge } from "../components/Badge";
import { EmptyState } from "../components/EmptyState";
import { JsonView } from "../components/JsonView";
import { Section } from "../components/Section";
import { Skeleton } from "../components/Skeleton";
import { Delta, Table, Td, Th } from "../components/Table";
import { ErrorBox } from "../components/Tables";
import { Term } from "../components/Term";
import { fmtPct } from "../format";
import { useApp, useLoad } from "../hooks";

type Metrics = { accuracy: number | null; baseline: number | null; alpha: number | null; n_test: number | null };
type Model = {
  name: string;
  version: string;
  saved_at: string;
  promoted: boolean;
  promote_reason: string | null;
  market: string | null;
  rows: number | null;
  train_range: [string, string] | null;
  backend: string | null;
  metrics: Metrics;
};
type ListResp = { models: Model[]; active: Model | null; note: string };
type PolicySummary = { n: number; return_mean: number | null; return_std: number | null; mdd_mean: number | null; sortino_mean: number | null };
type Experiment = {
  version: string;
  saved_at: string | null;
  backend: string | null;
  source: string | null;
  steps: number | null;
  seeds: number | null;
  promoted: boolean;
  promote_reason: string | null;
  summary: Partial<Record<"rl" | "fixed" | "llm" | "vol_target", PolicySummary>> | null;
};
type ExperimentsResp = { experiments: Experiment[]; active: Experiment | null };
type ShadowRow = {
  id: number;
  created_at: string;
  decision_date: string | null;
  ticker: string | null;
  market: string | null;
  model_version: string;
  llm_weight: number;
  proposed_weight: number;
  applied: boolean;
  realized_rl: number | null;
  realized_llm: number | null;
};
type ShadowResp = { rows: ShadowRow[] };
type Detail = Model & {
  importances: Record<string, number>;
  features: string[];
  metrics: Metrics & { folds: Array<{ index: number; train_rows: number; test_rows: number; accuracy: number; baseline: number; alpha: number }> };
};

const num4 = (v: number | null | undefined) => (v == null ? "—" : v.toFixed(4));
const num3 = (v: number | null | undefined) => (v == null ? "—" : v.toFixed(3));
const promoted = (p: boolean) => (p ? <Badge kind="ok" value="승격" /> : <Badge kind="neutral" value="미승격" />);

function RlSection() {
  const { accountId, tick } = useApp();
  const exps = useLoad(() => getJson<ExperimentsResp>("/api/rl/experiments"), [tick]);
  const shadow = useLoad(() => (accountId ? getJson<ShadowResp>(withAccount("/api/rl/shadow?days=60", accountId)) : null), [accountId, tick]);
  const cell = (s: PolicySummary | undefined) =>
    s ? (
      <>
        {fmtPct(s.return_mean)}
        {s.return_std != null && <span className="muted"> ±{(s.return_std * 100).toFixed(2)}</span>}
      </>
    ) : (
      "—"
    );
  return (
    <>
      <Section title="RL 사이저 실험" aside="모의 계좌 한정 · 승격은 `trader rl promote` 수동">
        <p className="note">워크포워드 × 시드 테스트 창 수익 평균. 승격해도 실전 계좌는 영향을 받지 않는다.</p>
        <ErrorBox error={exps.error} />
        {exps.loading && !exps.data ? (
          <Skeleton rows={2} cols={8} />
        ) : exps.data?.experiments.length ? (
          <Table dense caption="RL 실험">
            <thead>
              <tr>
                <Th>버전</Th>
                <Th>백엔드</Th>
                <Th>소스</Th>
                <Th num>스텝</Th>
                <Th num>시드</Th>
                <Th num>RL</Th>
                <Th num>고정</Th>
                <Th num>LLM</Th>
                <Th num>변동성 타깃</Th>
                <Th num>MDD(RL)</Th>
                <Th>승격</Th>
              </tr>
            </thead>
            <tbody>
              {exps.data.experiments.map((e) => (
                <tr key={e.version}>
                  <Td>{e.version}</Td>
                  <Td>{e.backend ?? "—"}</Td>
                  <Td>{e.source ?? "—"}</Td>
                  <Td num>{e.steps ?? "—"}</Td>
                  <Td num>{e.seeds ?? "—"}</Td>
                  <Td num>{cell(e.summary?.rl)}</Td>
                  <Td num>{cell(e.summary?.fixed)}</Td>
                  <Td num>{cell(e.summary?.llm)}</Td>
                  <Td num>{cell(e.summary?.vol_target)}</Td>
                  <Td num>{fmtPct(e.summary?.rl?.mdd_mean)}</Td>
                  <Td>{promoted(e.promoted)}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        ) : (
          <EmptyState title="RL 실험이 없어요" hint="`uv run trader rl train` 으로 학습하면 여기에 비교표가 쌓여요." />
        )}
      </Section>
      <Section title="섀도 기록" aside={`최근 60일 · ${accountId || "계좌 선택"}`}>
        <ErrorBox error={shadow.error} />
        {shadow.loading && !shadow.data ? (
          <Skeleton rows={2} cols={8} />
        ) : shadow.data?.rows.length ? (
          <Table dense caption="RL 섀도 기록">
            <thead>
              <tr>
                <Th>결정일</Th>
                <Th>종목</Th>
                <Th>모델</Th>
                <Th num>LLM 비중</Th>
                <Th num>제안 비중</Th>
                <Th>적용</Th>
                <Th num>실현 RL</Th>
                <Th num>실현 LLM</Th>
              </tr>
            </thead>
            <tbody>
              {shadow.data.rows.map((r) => (
                <tr key={r.id}>
                  <Td>{r.decision_date ?? r.created_at.slice(0, 10)}</Td>
                  <Td>
                    <span className="nm">
                      {r.ticker ?? "—"}
                      <small>{r.market ?? ""}</small>
                    </span>
                  </Td>
                  <Td>{r.model_version}</Td>
                  <Td num>{num3(r.llm_weight)}</Td>
                  <Td num>{num3(r.proposed_weight)}</Td>
                  <Td>{r.applied ? <Badge kind="ok" value="적용" /> : <Badge kind="neutral" value="섀도" />}</Td>
                  <Td num>
                    <Delta value={r.realized_rl == null ? null : r.realized_rl * 100} pct />
                  </Td>
                  <Td num>
                    <Delta value={r.realized_llm == null ? null : r.realized_llm * 100} pct />
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        ) : (
          <EmptyState title="섀도 기록이 없어요" hint="ml.yaml 의 rl_sizer.enabled 가 켜지고 승격 모델이 있을 때 모의 계좌 BUY/ADD 마다 1행씩 쌓여요." />
        )}
      </Section>
    </>
  );
}

export function MlPage() {
  const { tick } = useApp();
  const [sel, setSel] = useState<string | null>(null);
  const list = useLoad(() => getJson<ListResp>("/api/ml/models"), [tick]);
  const detail = useLoad(() => (sel ? getJson<Detail>(`/api/ml/models/${sel}`) : null), [sel, tick]);
  const imps = Object.entries(detail.data?.importances ?? {})
    .sort((a, b) => b[1] - a[1])
    .slice(0, 15);
  const maxImp = Math.max(...imps.map(([, v]) => v), 1e-9);
  const active = list.data?.active;
  return (
    <>
      <header className="page-h">
        <h1>ML</h1>
        <p className="sub">
          {active ? (
            <>
              활성 퀀트 모델 <b>{active.version}</b> · 정확도 {num4(active.metrics.accuracy)} · 알파 {num4(active.metrics.alpha)}
            </>
          ) : (
            "승격된 퀀트 모델이 없어요 — ML 랭킹 없이 파이프라인이 돕니다. `uv run trader ml train` 뒤 기준을 넘으면 자동 승격."
          )}
          {list.data?.note ? <span className="muted"> · {list.data.note}</span> : null}
        </p>
      </header>
      <ErrorBox error={list.error} />
      <div className="cols-2">
        <Section title="퀀트 모델" aside={`${list.data?.models.length ?? 0}개 · 행을 고르세요`}>
          {list.loading && !list.data ? (
            <Skeleton rows={4} cols={6} />
          ) : list.data?.models.length ? (
            <Table dense caption="퀀트 모델 목록">
              <thead>
                <tr>
                  <Th>버전</Th>
                  <Th>시장</Th>
                  <Th num>행</Th>
                  <Th num>정확도</Th>
                  <Th num>기준선</Th>
                  <Th num>알파</Th>
                  <Th>승격</Th>
                </tr>
              </thead>
              <tbody>
                {list.data.models.map((m) => (
                  <tr key={m.version} className={sel === m.version ? "sel clickable" : "clickable"} aria-selected={sel === m.version} onClick={() => setSel(m.version)}>
                    <Td>{m.version}</Td>
                    <Td>{m.market ?? "전체"}</Td>
                    <Td num>{m.rows ?? "—"}</Td>
                    <Td num>{num4(m.metrics.accuracy)}</Td>
                    <Td num>{num4(m.metrics.baseline)}</Td>
                    <Td num>{num4(m.metrics.alpha)}</Td>
                    <Td>{promoted(m.promoted)}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          ) : (
            <EmptyState title="학습된 모델이 없어요" hint="`uv run trader ml train` 이 피처 스토어로 워크포워드 학습을 돌려요." />
          )}
        </Section>
        <Section title={detail.data ? `quant ${detail.data.version}` : "모델 상세"} aside={detail.data?.backend ?? undefined}>
          <ErrorBox error={detail.error} />
          {detail.loading && !detail.data ? (
            <Skeleton rows={4} cols={5} />
          ) : detail.data ? (
            <>
              <p className="note">{detail.data.promote_reason}</p>
              <h3 className="sub-h">폴드별 성능</h3>
              <Table dense caption="폴드별 성능">
                <thead>
                  <tr>
                    <Th num>#</Th>
                    <Th num>학습 행</Th>
                    <Th num>테스트 행</Th>
                    <Th num>정확도</Th>
                    <Th num>기준선</Th>
                    <Th num>알파</Th>
                  </tr>
                </thead>
                <tbody>
                  {(detail.data.metrics.folds ?? []).map((f) => (
                    <tr key={f.index}>
                      <Td num>{f.index}</Td>
                      <Td num>{f.train_rows}</Td>
                      <Td num>{f.test_rows}</Td>
                      <Td num>{num4(f.accuracy)}</Td>
                      <Td num>{num4(f.baseline)}</Td>
                      <Td num>{num4(f.alpha)}</Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
              <h3 className="sub-h">피처 중요도 (permutation, 상위 15)</h3>
              <ul className="bars">
                {imps.map(([k, v]) => (
                  <li key={k}>
                    <span className="k">{k}</span>
                    <span className="bar" style={{ width: `${Math.max(2, (v / maxImp) * 100)}%` }} />
                    <span className="num muted">{v.toFixed(4)}</span>
                  </li>
                ))}
              </ul>
              <details>
                <summary className="muted">메타 전체</summary>
                <JsonView value={detail.data} depth={1} />
              </details>
            </>
          ) : (
            <EmptyState title="왼쪽에서 모델을 고르세요" />
          )}
        </Section>
      </div>
      <p className="note">
        <Term k="calibration" /> 은 만기 결정 60건부터 켜지고, 라벨은 같은 날 같은 시장 안 상대 순위 삼분위다.
      </p>
      <RlSection />
    </>
  );
}
