import { JsonView } from "./JsonView";
import { type ReportBlock, stageReport } from "../report";

/** 단계 파일을 보고서(제목·한 줄 요지·정의 목록·목록·문단)로. 원본 JSON 은 접힌 '원본 보기' 로만. */
export function StageReport({ stage, data, call, blocks: given }: { stage?: string; data: unknown; call?: unknown; blocks?: ReportBlock[] }) {
  const blocks = given ?? stageReport(stage ?? "", data);
  return (
    <div className="report">
      {blocks.length === 0 && <p className="muted">보고서로 바꿀 내용이 없어요. 아래 원본을 확인하세요.</p>}
      {blocks.map((b, i) => (
        <section key={`${b.title}-${i}`} className="report-block">
          <h3 className="report-h">{b.title}</h3>
          {b.lead && <p className="report-lead">{b.lead}</p>}
          {b.rows && b.rows.length > 0 && (
            <dl className="kv">
              {b.rows.map(([k, v]) => (
                <div key={k} className="kv-row">
                  <dt>{k}</dt>
                  <dd>{v}</dd>
                </div>
              ))}
            </dl>
          )}
          {b.list && b.list.length > 0 && (
            <ul className="report-list">
              {b.list.map((x, j) => (
                <li key={j}>{x}</li>
              ))}
            </ul>
          )}
          {b.paragraphs?.map((p, j) => (
            <p key={j} className="report-text">
              {p}
            </p>
          ))}
        </section>
      ))}
      <details className="report-raw">
        <summary>원본 보기</summary>
        <JsonView value={data} depth={1} />
        {call ? (
          <>
            <h4 className="sub-h">호출 정보(모델·비용·소요)</h4>
            <JsonView value={call} depth={1} />
          </>
        ) : null}
      </details>
    </div>
  );
}
