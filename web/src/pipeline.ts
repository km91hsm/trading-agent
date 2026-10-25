// 오늘의 실행: 체크포인트 파일명 목록을 파이프라인 순서의 '판' 으로 묶고, 단계마다 '보탠 것' 한 줄을 만든다.
import { termLabel } from "./glossary";

export type StageKey =
  | "00_inputs"
  | "05_quant"
  | "10_analyst"
  | "20_debate"
  | "25_research_summary"
  | "30_decision"
  | "31_decision"
  | "35_calibration"
  | "40_risk_check"
  | "41_risk_check"
  | "45_stop_loss"
  | "50_order"
  | "51_order"
  | "55_order_sl";

export type Stage = { key: StageKey; label: string; done: boolean; files: string[]; optional: boolean };

const ORDER: Array<{ key: StageKey; test: (s: string) => boolean; optional?: boolean }> = [
  { key: "00_inputs", test: (s) => s === "00_inputs" },
  { key: "05_quant", test: (s) => s === "05_quant" },
  { key: "10_analyst", test: (s) => s.startsWith("10_analyst_") },
  { key: "20_debate", test: (s) => s.startsWith("20_debate_") },
  { key: "25_research_summary", test: (s) => s === "25_research_summary" },
  { key: "35_calibration", test: (s) => s === "35_calibration", optional: true },
  { key: "30_decision", test: (s) => s === "30_decision" },
  { key: "40_risk_check", test: (s) => s.startsWith("40_risk_check_") },
  { key: "50_order", test: (s) => s.startsWith("50_order_") },
  { key: "31_decision", test: (s) => /^31_decision_r\d+$/.test(s), optional: true },
  { key: "41_risk_check", test: (s) => s.startsWith("41_risk_check_"), optional: true },
  { key: "51_order", test: (s) => s.startsWith("51_order_"), optional: true },
  { key: "45_stop_loss", test: (s) => s.startsWith("45_stop_loss_"), optional: true },
  { key: "55_order_sl", test: (s) => s.startsWith("55_order_sl_"), optional: true },
];

/** 항상 보이는 판 9개 + 파일이 있을 때만 붙는 선택 판. 파일은 이름순. */
export function pipelineStages(files: string[]): Stage[] {
  const sorted = [...files].sort();
  const out: Stage[] = [];
  for (const o of ORDER) {
    const mine = sorted.filter(o.test);
    if (o.optional && !mine.length) continue;
    out.push({ key: o.key, label: termLabel(`stage.${o.key}`), done: mine.length > 0, files: mine, optional: !!o.optional });
  }
  return out;
}

type Rec = Record<string, unknown>;
const rec = (v: unknown): Rec => (v && typeof v === "object" ? (v as Rec) : {});
const num = (v: unknown): number | null => (typeof v === "number" ? v : typeof v === "string" && v !== "" && Number.isFinite(Number(v)) ? Number(v) : null);
const pct = (v: unknown): string => {
  const n = num(v);
  return n === null ? "—" : `${Math.round(n * 100)}%`;
};
const count = (v: unknown): number => (Array.isArray(v) ? v.length : v && typeof v === "object" ? Object.keys(v as Rec).length : 0);

/** 파일별 payload(`data`) 를 받아 단계가 보탠 것을 한 줄로. 파일 이름은 계좌 접미 등 표시용. */
export function stageSummary(key: StageKey, items: Array<{ file: string; data: unknown }>): string {
  if (!items.length) return "";
  const d0 = rec(items[0].data);
  switch (key) {
    case "00_inputs":
      return `오류 절 ${count(d0.errors)}건`;
    case "05_quant": {
      const p = num(d0.p_up);
      return p === null ? "퀀트 확률 있음" : `상승 확률 ${pct(p)}`;
    }
    case "10_analyst": {
      const c = { BULLISH: 0, NEUTRAL: 0, BEARISH: 0 } as Record<string, number>;
      for (const it of items) {
        const s = String(rec(it.data).stance ?? "").toUpperCase();
        if (s in c) c[s] += 1;
      }
      return `강세 ${c.BULLISH} · 중립 ${c.NEUTRAL} · 약세 ${c.BEARISH}`;
    }
    case "20_debate": {
      const rounds = new Set(items.map((it) => String(rec(it.data).round ?? "")));
      return `${rounds.size}라운드 · ${items.length}턴`;
    }
    case "25_research_summary":
      return d0.sycophantic ? "붕괴 · r0 우선" : `정상 수렴${d0.preferred ? ` · ${String(d0.preferred)}` : ""}`;
    case "30_decision":
    case "31_decision":
      return `${termLabel(`action.${String(d0.action ?? "")}`)} · 비중 ${pct(d0.target_weight)} · 확신 ${num(d0.confidence)?.toFixed(2) ?? "—"}`;
    case "35_calibration":
      return d0.active === false ? "비활성" : "활성";
    case "40_risk_check":
    case "41_risk_check":
    case "45_stop_loss":
      return items
        .map((it) => {
          const d = rec(it.data);
          const v = rec(d.verdict ?? d);
          const acct = it.file.replace(/^\d\d_[a-z_]+?_(?=[^_]+$)/, "");
          if (v.approved) return `${acct}: 승인 ${num(v.adjusted_qty) ?? 0}주`;
          const reasons = Array.isArray(v.reasons) ? (v.reasons as unknown[]) : [];
          return `${acct}: 거부${reasons.length ? ` · ${String(reasons[0])}` : ""}`;
        })
        .join(" / ");
    case "50_order":
    case "51_order":
    case "55_order_sl":
      return items
        .map((it) => {
          const d = rec(it.data);
          const acct = it.file.replace(/^\d\d_[a-z_]+?_(?=[^_]+$)/, "");
          return `${acct}: ${termLabel(`status.${String(d.status ?? "")}`)}`;
        })
        .join(" / ");
  }
  return "";
}

// --- 오늘 스캔(`_scan.json` 요약, /api/today.scan) -------------------------------------
export type ScanPick = { ticker: string; reason: string | null; discovery: { tier: string; catalyst: string } | null };
export type ScanSummary = {
  picks: ScanPick[];
  targets: number;
  cap_hit: boolean;
  discovery: { candidates: unknown[]; universe: number; listed: number; unresolved: number } | null;
  discovery_line: string;
};

/** picks 한 행의 사유 문구: 뉴스 발굴이면 "뉴스 발굴(유니버스 밖) · <촉매>" 를 screener 사유 앞에 붙인다. */
export function pickReason(p: ScanPick): string {
  const base = p.reason?.trim() || "사유 없음";
  if (!p.discovery) return base;
  const tier = p.discovery.tier === "listed" ? "뉴스 발굴(유니버스 밖)" : "뉴스 발굴";
  const cat = p.discovery.catalyst?.trim();
  return `${tier}${cat ? ` · ${cat}` : ""} — ${base}`;
}

/** 스캔 머리 한 줄: "picks 5 · 분석 대상 7 · 뉴스 발굴 3(…)" (+ 세션 상한) */
export function scanHeadline(s: ScanSummary): string {
  const parts = [`picks ${s.picks.length}`, `분석 대상 ${s.targets}`, s.discovery_line];
  if (s.cap_hit) parts.push("세션 상한");
  return parts.join(" · ");
}
