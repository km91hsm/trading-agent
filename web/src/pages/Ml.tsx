import { useState } from "react";
import { getJson, withAccount } from "../api";
import { JsonView } from "../components/JsonView";
import { ErrorBox } from "../components/Tables";
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

const pct = (v: number | null | undefined) => (v == null ? "-" : `${(v * 100).toFixed(2)}%`);
const w = (v: number | null | undefined) => (v == null ? "-" : v.toFixed(3));

function RlSection() {
  const { accountId, tick } = useApp();
  const exps = useLoad(() => getJson<ExperimentsResp>("/api/rl/experiments"), [tick]);
  const shadow = useLoad(
    () => (accountId ? getJson<ShadowResp>(withAccount("/api/rl/shadow?days=60", accountId)) : null),
    [accountId, tick],
  );
  const cell = (s: PolicySummary | undefined) =>
    s ? `${pct(s.return_mean)}${s.return_std != null ? ` ±${(s.return_std * 100).toFixed(2)}` : ""}` : "-";
  return (
    <>
      <h2>RL 실험 (사이저, 모의 계좌 한정)</h2>
      <p className="muted">
        워크포워드 × 시드 테스트 창 수익 평균. 승격은 `uv run trader rl promote` 수동이며, 승격 후에도 실전 계좌는 영향을 받지 않는다.
      </p>
      <ErrorBox error={exps.error} />
      <table>
        <thead>
          <tr>
            <th>버전</th><th>백엔드</th><th>소스</th><th>스텝</th><th>시드</th><th>RL</th><th>고정</th><th>LLM</th><th>변동성 타깃</th><th>MDD(RL)</th><th>승격</th>
          </tr>
        </thead>
        <tbody>
          {(exps.data?.experiments ?? []).map((e) => (
            <tr key={e.version}>
              <td>{e.version}</td><td>{e.backend ?? "-"}</td><td>{e.source ?? "-"}</td>
              <td>{e.steps ?? "-"}</td><td>{e.seeds ?? "-"}</td>
              <td>{cell(e.summary?.rl)}</td><td>{cell(e.summary?.fixed)}</td>
              <td>{cell(e.summary?.llm)}</td><td>{cell(e.summary?.vol_target)}</td>
              <td>{pct(e.summary?.rl?.mdd_mean)}</td><td>{e.promoted ? "예" : "아니오"}</td>
            </tr>
          ))}
          {!exps.data?.experiments.length && (
            <tr><td colSpan={11} className="muted">실험 없음 — `uv run trader rl train`</td></tr>
          )}
        </tbody>
      </table>
      <h3>섀도 기록 (최근 60일, {accountId || "계좌 선택"})</h3>
      <ErrorBox error={shadow.error} />
      <table>
        <thead>
          <tr><th>결정일</th><th>종목</th><th>모델</th><th>LLM 비중</th><th>제안 비중</th><th>적용</th><th>실현 RL</th><th>실현 LLM</th></tr>
        </thead>
        <tbody>
          {(shadow.data?.rows ?? []).map((r) => (
            <tr key={r.id}>
              <td>{r.decision_date ?? r.created_at.slice(0, 10)}</td><td>{r.market ?? ""} {r.ticker ?? "-"}</td>
              <td>{r.model_version}</td><td>{w(r.llm_weight)}</td><td>{w(r.proposed_weight)}</td>
              <td>{r.applied ? "예" : "아니오"}</td><td>{pct(r.realized_rl)}</td><td>{pct(r.realized_llm)}</td>
            </tr>
          ))}
          {!shadow.data?.rows.length && (
            <tr><td colSpan={8} className="muted">기록 없음 (ml.yaml rl_sizer.enabled ∧ 승격 모델일 때 모의 계좌 BUY/ADD 마다 1행)</td></tr>
          )}
        </tbody>
      </table>
    </>
  );
}
type Detail = Model & {
  importances: Record<string, number>;
  features: string[];
  metrics: Metrics & { folds: Array<{ index: number; train_rows: number; test_rows: number; accuracy: number; baseline: number; alpha: number }> };
};

export function MlPage() {
  const { tick } = useApp();
  const [sel, setSel] = useState<string | null>(null);
  const list = useLoad(() => getJson<ListResp>("/api/ml/models"), [tick]);
  const detail = useLoad(() => (sel ? getJson<Detail>(`/api/ml/models/${sel}`) : null), [sel, tick]);
  const imps = Object.entries(detail.data?.importances ?? {}).sort((a, b) => b[1] - a[1]).slice(0, 15);
  const maxImp = Math.max(...imps.map(([, v]) => v), 1e-9);
  return (
    <section>
      <h1>ML</h1>
      <p className="muted">{list.data?.note}</p>
      <ErrorBox error={list.error} />
      {list.data?.active ? (
        <p>
          활성 모델: <b>quant {list.data.active.version}</b> · 정확도 {list.data.active.metrics.accuracy} · 알파{" "}
          {list.data.active.metrics.alpha}
        </p>
      ) : (
        <p className="muted">승격된 모델 없음 — `uv run trader ml train` 후 기준 통과 시 자동 승격</p>
      )}
      <div className="split">
        <table>
          <thead>
            <tr>
              <th>버전</th><th>시장</th><th>행</th><th>정확도</th><th>기준선</th><th>알파</th><th>승격</th>
            </tr>
          </thead>
          <tbody>
            {(list.data?.models ?? []).map((m) => (
              <tr key={m.version} className={sel === m.version ? "sel" : ""} onClick={() => setSel(m.version)}>
                <td>{m.version}</td><td>{m.market ?? "전체"}</td><td>{m.rows ?? "-"}</td>
                <td>{m.metrics.accuracy ?? "-"}</td><td>{m.metrics.baseline ?? "-"}</td>
                <td>{m.metrics.alpha ?? "-"}</td><td>{m.promoted ? "예" : "아니오"}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div>
          <ErrorBox error={detail.error} />
          {detail.data ? (
            <>
              <h3>quant {detail.data.version} <small>{detail.data.backend}</small></h3>
              <p className="muted">{detail.data.promote_reason}</p>
              <h4>폴드별 성능</h4>
              <table>
                <thead>
                  <tr><th>#</th><th>학습 행</th><th>테스트 행</th><th>정확도</th><th>기준선</th><th>알파</th></tr>
                </thead>
                <tbody>
                  {(detail.data.metrics.folds ?? []).map((f) => (
                    <tr key={f.index}>
                      <td>{f.index}</td><td>{f.train_rows}</td><td>{f.test_rows}</td>
                      <td>{f.accuracy}</td><td>{f.baseline}</td><td>{f.alpha}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <h4>피처 중요도 (permutation, 상위 15)</h4>
              <ul className="bars">
                {imps.map(([k, v]) => (
                  <li key={k}>
                    <span className="k">{k}</span>
                    <span className="bar" style={{ width: `${Math.max(2, (v / maxImp) * 100)}%` }} />
                    <span className="muted">{v.toFixed(4)}</span>
                  </li>
                ))}
              </ul>
              <details>
                <summary>메타 전체</summary>
                <JsonView value={detail.data} depth={1} />
              </details>
            </>
          ) : (
            <p className="muted">왼쪽에서 모델을 고르세요</p>
          )}
        </div>
      </div>
      <RlSection />
    </section>
  );
}
