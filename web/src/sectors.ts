// 섹터 비중 표 (Phase 17 S): /api/portfolio 의 sectors → 행. 막대 폭·상한 초과 표시는 순수 함수.

export type SectorRow = { sector: string; eval_amount: string; weight: number | null; tickers: string[]; count: number };

export type SectorView = {
  key: string;
  sector: string;
  count: number;
  eval_amount: string;
  weight: number | null; // 0~1
  pct: string; // "45.0%"
  barPct: number; // 0~100 (막대 폭)
  over: boolean; // 상한 초과 (헤지·현금은 상한 대상 아님)
  capped: boolean; // 상한 규칙 대상(일반 섹터)인가
  tickers: string[];
};

const EXEMPT = new Set(["현금", "헤지"]);

export function sectorRows(rows: SectorRow[], maxSectorPct: number): SectorView[] {
  return rows.map((r) => {
    const w = r.weight;
    const capped = !EXEMPT.has(r.sector);
    return {
      key: r.sector,
      sector: r.sector,
      count: r.count,
      eval_amount: r.eval_amount,
      weight: w,
      pct: w == null ? "—" : `${(w * 100).toFixed(1)}%`,
      barPct: w == null ? 0 : Math.max(0, Math.min(100, w * 100)),
      over: capped && w != null && w > maxSectorPct + 1e-9,
      capped,
      tickers: r.tickers,
    };
  });
}

/** 상한선 x 위치(0~100). 막대와 같은 축. */
export function capLinePct(maxSectorPct: number): number {
  return Math.max(0, Math.min(100, maxSectorPct * 100));
}

// --- 국면 (Phase 17 H) -----------------------------------------------------------------

export type RegimeMarket = {
  market: string;
  state: "bull" | "neutral" | "bear" | null;
  count: number | null;
  since: string | null;
  asof: string | null;
  signals: { ma_below: boolean | null; momentum_weak: boolean | null; vol_high: boolean | null };
  /** 오늘 시장 세션(거시 애널리스트·토론·전략가)의 헤지 제안. 없으면 null (Phase 18). */
  view?: { stance: "RISK_ON" | "NEUTRAL" | "RISK_OFF"; hedge_weight: number; leveraged: boolean; confidence: number } | null;
};
export type RegimeResp = {
  enabled: boolean;
  max_weight_pct: number;
  leverage: boolean;
  bear_min_signals: number;
  market_session?: boolean;
  min_weight_pct?: number;
  markets: RegimeMarket[];
};

export const STANCE_LABEL: Record<string, string> = { RISK_ON: "위험 선호", NEUTRAL: "중립", RISK_OFF: "위험 회피" };

/** 칩 꼬리: "제안 12%" (bear 시장에 오늘 제안이 있을 때만). 없으면 "". */
export function proposalText(r: RegimeResp | null): string {
  if (!r?.enabled) return "";
  const bears = r.markets.filter((m) => m.state === "bear" && m.view);
  if (!bears.length) return "";
  return bears.map((m) => `${m.market} 제안 ${Math.round((m.view!.hedge_weight ?? 0) * 100)}%`).join(" · ");
}

export const REGIME_LABEL: Record<string, string> = { bull: "상승", neutral: "중립", bear: "하락" };
const SIGNAL_LABEL: Record<string, string> = { ma_below: "200일선 아래", momentum_weak: "20일 수익률 약세", vol_high: "변동성 높음" };

/** 상태줄 칩: "KR 하락 2/3 · US 상승 0/3". 기록 없으면 "판정 전". level 은 bear → err, neutral → warn. */
export function regimeText(r: RegimeResp | null): { text: string; level: "ok" | "warn" | "err" | "none"; title: string } {
  if (!r) return { text: "확인 중", level: "none", title: "" };
  if (!r.enabled) return { text: "헤지 꺼짐", level: "none", title: "settings.yaml hedge.enabled=false" };
  const known = r.markets.filter((m) => m.state);
  if (!known.length) return { text: "판정 전", level: "none", title: "scan 앞에서 매일 판정해요" };
  const text = known.map((m) => `${m.market} ${REGIME_LABEL[m.state!] ?? m.state} ${m.count ?? 0}/3`).join(" · ");
  const level = known.some((m) => m.state === "bear") ? "err" : known.some((m) => m.state === "neutral") ? "warn" : "ok";
  const title = known
    .map((m) => {
      const on = Object.entries(m.signals)
        .filter(([, v]) => v)
        .map(([k]) => SIGNAL_LABEL[k] ?? k);
      const v = m.view ? ` · 시장 세션 ${STANCE_LABEL[m.view.stance] ?? m.view.stance} ${Math.round(m.view.hedge_weight * 100)}%` : "";
      return `${m.market}: ${on.length ? on.join(", ") : "지표 없음"}${m.since ? ` (${m.since}~)` : ""}${v}`;
    })
    .join(" / ");
  return { text, level, title };
}
