// 테마 열기 (Phase 25, /api/system/theme) → 표 행. 순수 함수.
export type ThemeTicker = { ticker: string; name: string | null; why: string[] };
export type HeatTheme = {
  theme: string;
  rank: number;
  score: number;
  components: Record<string, number>;
  tickers: ThemeTicker[];
  catalyst_ok: boolean;
};
export type ThemeResp = { enabled: boolean; top_n: number; market: string; date: string | null; observed_at: string | null; themes: HeatTheme[]; signals: Record<string, unknown> };

const COMP_LABEL: Record<string, string> = { news: "새 뉴스", rank: "순위", flow: "수급", saveticker: "세이브티커", vol: "거래량" };

/** '섹터:반도체' → '반도체 (섹터)', 'kw:로봇' → '로봇 (뉴스 키워드)'. */
export function themeName(t: string): string {
  const i = t.indexOf(":");
  if (i < 0) return t;
  const kind = t.slice(0, i);
  return `${t.slice(i + 1)} (${kind === "섹터" ? "섹터" : "뉴스 키워드"})`;
}

export type HeatRow = { key: string; rank: number; name: string; score: string; catalyst: string; drivers: string; tickers: string; top: boolean };

/** 상위 top_n 은 top=true. 성분은 0.3 이상만 '새 뉴스·순위' 처럼 이어 붙인다. */
export function heatRows(r: ThemeResp | null | undefined, limit = 8): HeatRow[] {
  if (!r) return [];
  return r.themes.slice(0, limit).map((t) => ({
    key: t.theme,
    rank: t.rank,
    name: themeName(t.theme),
    score: t.score.toFixed(2),
    catalyst: t.catalyst_ok ? "있음" : "없음 (커뮤니티뿐)",
    drivers:
      Object.entries(t.components)
        .filter(([, v]) => v >= 0.3)
        .sort((a, b) => b[1] - a[1])
        .map(([k]) => COMP_LABEL[k] ?? k)
        .join("·") || "—",
    tickers: t.tickers
      .slice(0, 3)
      .map((x) => (x.name ? `${x.name}(${x.ticker})` : x.ticker))
      .join(", "),
    top: t.rank <= r.top_n,
  }));
}
