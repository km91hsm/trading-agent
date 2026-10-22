// 로딩 스켈레톤: 표 행 모양으로 자리만 잡는다.
export function Skeleton({ rows = 4, cols = 5 }: { rows?: number; cols?: number }) {
  return (
    <div className="skeleton" aria-busy="true" aria-label="불러오는 중">
      {Array.from({ length: rows }, (_, r) => (
        <div className="skeleton-row" key={r}>
          {Array.from({ length: cols }, (_, c) => (
            <span className="skeleton-cell" key={c} style={{ flex: c === 0 ? 2 : 1 }} />
          ))}
        </div>
      ))}
    </div>
  );
}
