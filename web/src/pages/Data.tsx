import { TodayPage } from "./Today";

export function DataPage() {
  return (
    <>
      <TodayPage title="데이터 탐색 (00_inputs)" inputsOnly />
      <section>
        <h2>피처 스토어</h2>
        <p className="muted">state/features parquet 탐색은 Phase 7(ML)에서 추가한다.</p>
      </section>
    </>
  );
}
