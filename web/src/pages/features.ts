/** 피처 스토어 탐색 페이지의 순수 헬퍼 (정렬·표시). API 형태는 /api/features/* 와 같다. */

export type Cell = string | number | boolean | null;
export type FeatureRow = Record<string, Cell>;
export type LatestResp = { market: string; asof: string | null; columns: string[]; rows: FeatureRow[] };
export type HistoryResp = { ticker: string; market: string; rows: FeatureRow[] };

export type SortDir = "asc" | "desc";

/** key 열로 정렬한 새 배열. null 은 방향과 무관하게 항상 마지막 (결측이 '최대' 로 보이지 않게). */
export function sortRows<T extends FeatureRow>(rows: T[], key: string, dir: SortDir): T[] {
  const sign = dir === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => {
    const x = a[key];
    const y = b[key];
    if (x == null && y == null) return 0;
    if (x == null) return 1;
    if (y == null) return -1;
    if (typeof x === "number" && typeof y === "number") return (x - y) * sign;
    return String(x).localeCompare(String(y)) * sign;
  });
}

/** 숫자는 소수 4자리(정수는 그대로), null 은 "-", 그 밖은 문자열. */
export function fmt(v: Cell | undefined): string {
  if (v == null) return "-";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(4);
  return String(v);
}

/** 최신 표에 보여 줄 열 (전부 보이면 너무 넓다). 이력 표는 같은 열을 날짜와 함께 쓴다. */
export const KEY_COLUMNS = [
  "ticker",
  "sector",
  "close",
  "roc_10",
  "high_52w_pos",
  "op_margin",
  "roe",
  "debt_ratio",
  "op_margin_vs_sector",
  "roe_vs_sector",
  "debt_ratio_vs_sector",
  "flow_foreign_20d",
  "news_count",
];
