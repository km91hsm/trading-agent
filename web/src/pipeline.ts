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
  | "55_order_sl"
  | "43_rebalance"
  | "53_order_rebalance";

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
  { key: "43_rebalance", test: (s) => s.startsWith("43_rebalance_"), optional: true },
  { key: "53_order_rebalance", test: (s) => s.startsWith("53_order_rebalance_"), optional: true },
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
      // relative_tercile(기본)은 상대 순위 상위 1/3 확률이지 상승 확률이 아니다
      const what = String(d0.label_mode ?? "relative_tercile") === "absolute" ? "상승" : "상위 1/3";
      return p === null ? "퀀트 확률 있음" : `${what} 확률 ${pct(p)}`;
    }
    case "10_analyst": {
      const c = { BULLISH: 0, NEUTRAL: 0, BEARISH: 0 } as Record<string, number>;
      for (const it of items) {
        // 애널리스트는 `signal`(STRONG_BUY..STRONG_SELL) 을 쓴다. 옛 파일의 `stance` 도 받는다.
        const d = rec(it.data);
        const sig = String(d.signal ?? "").toUpperCase();
        const s = sig.endsWith("BUY") ? "BULLISH" : sig.endsWith("SELL") ? "BEARISH" : sig === "NEUTRAL" ? "NEUTRAL" : String(d.stance ?? "").toUpperCase();
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
    case "43_rebalance":
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
    case "53_order_rebalance":
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

/** 장중 뉴스 발굴(Phase 23)이 낸 요청 한 행 — /api/today.news_jobs. */
export type NewsJob = {
  id: number;
  kind: string;
  ticker: string | null;
  status: string;
  requested_at: string;
  finished_at: string | null;
  error: string | null;
};

const NEWS_JOB_KIND: Record<string, string> = {
  analyze_ticker: "종목 분석(새 뉴스)",
  reevaluate: "재평가(새 뉴스)",
  discover: "유니버스 밖 발굴",
};

const NEWS_JOB_STATUS: Record<string, string> = {
  queued: "대기",
  running: "실행 중",
  ok: "완료",
  error: "오류",
  skipped: "건너뜀",
};

export type NewsJobRow = { id: number; kindLabel: string; ticker: string | null; stateLabel: string; pending: boolean; failed: boolean; at: string; error: string | null };

/** 뉴스 발굴 요청 → 표 행(최신 먼저, 서버 순서 유지). */
export function newsJobRows(jobs: NewsJob[]): NewsJobRow[] {
  return jobs.map((j) => ({
    id: j.id,
    kindLabel: NEWS_JOB_KIND[j.kind] ?? j.kind,
    ticker: j.ticker,
    stateLabel: NEWS_JOB_STATUS[j.status] ?? j.status,
    pending: j.status === "queued" || j.status === "running",
    failed: j.status === "error",
    at: j.finished_at ?? j.requested_at,
    error: j.error,
  }));
}

/** '장중 발굴' 머리말: "요청 5 · 대기 1 · 오류 0". */
export function newsJobsHeadline(jobs: NewsJob[]): string {
  const rows = newsJobRows(jobs);
  return `요청 ${rows.length} · 대기 ${rows.filter((r) => r.pending).length} · 오류 ${rows.filter((r) => r.failed).length}`;
}

/** 분 단위 단타(Phase 23 S) — /api/system/scalp. */
export type ScalpTrade = {
  ticker: string;
  entry_price: string;
  entry_time: string;
  exit_price?: string;
  exit_time?: string;
  reason?: string;
  ret?: number;
  qty?: Record<string, number>;
  dry_run?: boolean;
};
export type ScalpResp = {
  enabled: boolean;
  max_weight_pct: number;
  max_positions: number;
  markets: Record<string, { date: string; open: ScalpTrade[]; closed: ScalpTrade[]; halted: string | null }>;
};

const SCALP_REASON: Record<string, string> = { take_profit: "익절", stop: "손절", max_hold: "보유 시간", close: "마감 전", eod: "장 마감" };

export type ScalpRow = { key: string; ticker: string; state: string; entry: string; exit: string; ret: string; reason: string; dry: boolean };

/** 상태 칩 문구: 끔 / 켬 · 보유 n · 청산 n (· 중단). */
export function scalpChip(r: ScalpResp | null, market: string): { text: string; level: "none" | "ok" | "warn" } {
  if (!r) return { text: "—", level: "none" };
  if (!r.enabled) return { text: "끔", level: "none" };
  const m = r.markets[market];
  if (!m) return { text: "켬", level: "ok" };
  const parts = [`켬 · 보유 ${m.open.length} · 청산 ${m.closed.length}`];
  if (m.halted) parts.push("중단");
  return { text: parts.join(" · "), level: m.halted ? "warn" : "ok" };
}

const hhmm = (t?: string) => (t && t.length >= 4 ? `${t.slice(0, 2)}:${t.slice(2, 4)}` : "—");

/** 오늘 단타 표 행: 보유 중 먼저, 그 다음 청산(최신 먼저). */
export function scalpRows(r: ScalpResp | null, market: string): ScalpRow[] {
  const m = r?.markets[market];
  if (!m) return [];
  const open = m.open.map((t) => ({
    key: `o:${t.ticker}:${t.entry_time}`,
    ticker: t.ticker,
    state: "보유 중",
    entry: `${t.entry_price} @ ${hhmm(t.entry_time)}`,
    exit: "—",
    ret: "—",
    reason: "",
    dry: !!t.dry_run,
  }));
  const closed = [...m.closed].reverse().map((t) => ({
    key: `c:${t.ticker}:${t.entry_time}`,
    ticker: t.ticker,
    state: "청산",
    entry: `${t.entry_price} @ ${hhmm(t.entry_time)}`,
    exit: `${t.exit_price ?? "—"} @ ${hhmm(t.exit_time)}`,
    ret: t.ret === undefined ? "—" : `${t.ret >= 0 ? "+" : ""}${(t.ret * 100).toFixed(2)}%`,
    reason: SCALP_REASON[t.reason ?? ""] ?? t.reason ?? "",
    dry: !!t.dry_run,
  }));
  return [...open, ...closed];
}
