import { useState } from "react";
import { getJson } from "../api";
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
    </section>
  );
}
