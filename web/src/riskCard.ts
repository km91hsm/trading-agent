// 포트폴리오 위험 카드(Stock D-088) — 서버 `/api/portfolio/risk` 응답을 시세표 행으로.
export type RiskResp = {
  insufficient?: string;
  missing?: string[];
  asof?: string;
  window?: number;
  n_obs?: number;
  var95?: number;
  cvar95?: number;
  beta?: number | null;
  corr?: { tickers: string[]; matrix: number[][] };
  contributions?: Array<{ ticker: string; weight: number; contribution: number }>;
};

export type RiskSummary = { label: string; value: string; note: string };

const pct = (v: number) => `${(v * 100).toFixed(2)}%`;

export function riskSummary(r: RiskResp): RiskSummary[] {
  if (r.insufficient || r.var95 === undefined || r.cvar95 === undefined) return [];
  return [
    { label: "하루 VaR 95%", value: pct(r.var95), note: "20일에 하루는 이보다 더 잃을 수 있어요(최근 수익 기준)" },
    { label: "CVaR 95%", value: pct(r.cvar95), note: "그런 나쁜 날들의 평균 손실" },
    {
      label: "베타",
      value: r.beta === null || r.beta === undefined ? "—" : r.beta.toFixed(2),
      note: "유니버스 균등(1/N) 대비 민감도 — 1 보다 크면 시장보다 크게 움직여요",
    },
  ];
}

/** 가장 높은 상관 쌍(대각 제외). 보유가 둘 미만이면 null. */
export function topCorrelation(r: RiskResp): { a: string; b: string; rho: number } | null {
  const c = r.corr;
  if (!c || c.tickers.length < 2) return null;
  let best: { a: string; b: string; rho: number } | null = null;
  for (let i = 0; i < c.tickers.length; i++) {
    for (let j = i + 1; j < c.tickers.length; j++) {
      const rho = c.matrix[i]?.[j];
      if (rho !== undefined && (best === null || rho > best.rho)) best = { a: c.tickers[i], b: c.tickers[j], rho };
    }
  }
  return best;
}
