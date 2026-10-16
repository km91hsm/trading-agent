import { TodayPage } from "./Today";

export function DataPage() {
  return (
    <>
      <TodayPage title="데이터 탐색 (00_inputs)" inputsOnly />
      <section>
        <h2>피처 스토어</h2>
        <p className="muted">피처 스토어(state/features parquet)는 ML 페이지의 모델 메타(피처 목록·중요도)로 확인한다. 행 단위 탐색은 Phase 8.</p>
      </section>
    </>
  );
}
