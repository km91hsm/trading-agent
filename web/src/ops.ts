/** 운영 점검(Phase 23 A) — /api/system/ops 응답과 특보판 행 변환(순수 함수). */

export type OpsLevel = "ok" | "watch" | "warning" | "critical";
export type OpsItem = { key: string; level: OpsLevel; text: string; link: string | null; count: number | null; extra?: Record<string, unknown> };
export type OpsResp = {
  level: OpsLevel;
  label: string;
  at: string;
  areas: Record<string, OpsLevel>;
  items: OpsItem[];
  actions: OpsItem[];
  headline: string;
  source: "daemon" | "live";
};

export const LEVEL_ORDER: OpsLevel[] = ["ok", "watch", "warning", "critical"];
export const LEVEL_LABEL: Record<OpsLevel, string> = { ok: "정상", watch: "관심", warning: "주의", critical: "경보" };
export const AREA_LABEL: Record<string, string> = { loss: "손익", orders: "주문", reconcile: "대조", jobs: "데몬·잡", llm: "세션" };

export type OpsRow = { key: string; area: string; level: OpsLevel; levelLabel: string; text: string; link: string | null; danger: boolean };

/** 항목 → 표 행, 심각한 것 먼저. */
export function opsRows(r: OpsResp | null): OpsRow[] {
  if (!r) return [];
  return [...r.items]
    .sort((a, b) => LEVEL_ORDER.indexOf(b.level) - LEVEL_ORDER.indexOf(a.level))
    .map((i) => ({
      key: i.key,
      area: AREA_LABEL[i.key.split(".")[0]] ?? i.key.split(".")[0],
      level: i.level,
      levelLabel: LEVEL_LABEL[i.level] ?? i.level,
      text: i.text,
      link: i.link,
      danger: i.level === "critical" || i.level === "warning",
    }));
}

/** 영역 요약: "손익 정상 · 주문 주의 · …" */
export function areaLine(r: OpsResp | null): string {
  if (!r) return "";
  return Object.entries(r.areas)
    .map(([k, v]) => `${AREA_LABEL[k] ?? k} ${LEVEL_LABEL[v] ?? v}`)
    .join(" · ");
}
