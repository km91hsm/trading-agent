import type { ShadowReport, ShadowStats } from "./types";

export type ShadowRow = {
  key: "candidates_1n" | "quant_topn" | "index_hold" | "index_trend";
  label: string;
  n: number;
  meanPct: number | null;
  lowPct: number | null;
  highPct: number | null;
  verdict: "부족" | "우위" | "열위" | "구분 안 됨";
};

const LABEL: Record<ShadowRow["key"], string> = {
  candidates_1n: "같은 후보 균등(1/N)",
  quant_topn: "정량 상위만",
  index_hold: "지수 보유",
  index_trend: "지수 추세(100일선)",
};

const pct = (v: number | null) => (v === null ? null : v * 100);

/** 만기 쌍 수가 기준 미만이면 '부족', 95% 구간이 0 을 넘으면 '우위', 0 아래면 '열위'. */
export function verdictOf(s: ShadowStats, minPairs: number): ShadowRow["verdict"] {
  if (s.n < minPairs || s.ci_low === null || s.ci_high === null) return "부족";
  if (s.ci_low > 0) return "우위";
  if (s.ci_high < 0) return "열위";
  return "구분 안 됨";
}

export function shadowRows(r: ShadowReport): ShadowRow[] {
  // 지수 기준선은 PR 이전 응답·만기 전이면 없을 수 있다 — 있는 것만
  return (["candidates_1n", "quant_topn", "index_hold", "index_trend"] as const).flatMap((key) => {
    const s = r.pairs[key];
    if (!s) return [];
    return {
      key,
      label: LABEL[key],
      n: s.n,
      meanPct: pct(s.mean_diff),
      lowPct: pct(s.ci_low),
      highPct: pct(s.ci_high),
      verdict: verdictOf(s, r.min_pairs),
    } as ShadowRow;
  });
}
