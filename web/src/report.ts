// 단계 파일(체크포인트) → 사람이 읽는 보고서 블록 (Phase 24). 원시 JSON 은 화면의 '원본 보기' 로만.
// 순수 함수라 vitest 로 형식을 고정한다. 모르는 단계는 최상위 값을 표로 펼치는 일반 블록.
import { termLabel } from "./glossary";

export type ReportBlock = {
  title: string;
  lead?: string;
  rows?: [string, string][];
  paragraphs?: string[];
  list?: string[];
};

type Rec = Record<string, unknown>;
const rec = (v: unknown): Rec => (v && typeof v === "object" && !Array.isArray(v) ? (v as Rec) : {});
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const str = (v: unknown): string => (v === null || v === undefined ? "" : typeof v === "string" ? v : typeof v === "number" || typeof v === "boolean" ? String(v) : "");
const num = (v: unknown): number | null => {
  if (v === null || v === undefined || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
};
const DASH = "—";
const pct = (v: unknown, digits = 0): string => {
  const n = num(v);
  return n === null ? DASH : `${(n * 100).toFixed(digits)}%`;
};
const conf = (v: unknown): string => {
  const n = num(v);
  return n === null ? DASH : n.toFixed(2);
};
const price = (v: unknown): string => {
  const n = num(v);
  return n === null ? DASH : n.toLocaleString("en-US", { maximumFractionDigits: 4 });
};
const texts = (v: unknown): string[] => arr(v).map((x) => (typeof x === "string" ? x : str(rec(x).claim) || str(rec(x).text) || str(rec(x).title))).filter(Boolean);

export const SIGNAL_LABEL: Record<string, string> = {
  STRONG_BUY: "강한 매수",
  BUY: "매수",
  NEUTRAL: "중립",
  SELL: "매도",
  STRONG_SELL: "강한 매도",
  UNKNOWN: "판단 불가",
};
export const STANCE_LABEL: Record<string, string> = { BULLISH: "강세", NEUTRAL: "중립", BEARISH: "약세", RISK_ON: "위험 선호", RISK_OFF: "위험 회피" };
const VALUATION_LABEL: Record<string, string> = { UNDERVALUED: "저평가", FAIR: "적정", OVERVALUED: "고평가", UNKNOWN: "판단 불가" };
const TIMING_LABEL: Record<string, string> = { NOW: "지금", CONDITIONAL: "조건부", WAIT: "대기" };
const ROLE_LABEL: Record<string, string> = { technical: "차트", fundamental: "회사가치", news_macro: "뉴스·거시", sentiment: "투자자 분위기", quant: "퀀트", market: "거시·시장" };
const HORIZON_LABEL: Record<string, string> = { short: "단기", mid: "중기", long: "장기" };
const SECTION_LABEL: Record<string, string> = { technical: "시세·차트", fundamental: "재무·공시", news_macro: "뉴스·거시", sentiment: "수급·커뮤니티" };

const label = (map: Record<string, string>, v: unknown) => map[str(v)] ?? (str(v) || DASH);
const acctOf = (stage: string) => stage.replace(/^\d\d_[a-z_]+?_(?=[^_]+$)/, "");

function analyst(d: Rec, title: string): ReportBlock[] {
  const out: ReportBlock[] = [
    {
      title,
      lead: str(d.summary) || undefined,
      rows: [
        ["신호", label(SIGNAL_LABEL, d.signal)],
        ["점수", num(d.score) === null ? DASH : (num(d.score) as number).toFixed(2)],
        ["확신", conf(d.confidence)],
        ["관점", label(HORIZON_LABEL, d.horizon)],
      ],
      list: texts(d.bullets),
    },
  ];
  const ev = texts(d.evidence).slice(0, 2);
  if (ev.length) out.push({ title: "근거", list: ev });
  const risks = [...texts(d.key_risks), ...texts(d.counter_evidence)];
  if (risks.length) out.push({ title: "위험·반대 증거", list: risks });
  if (texts(d.data_gaps).length) out.push({ title: "부족한 자료", list: texts(d.data_gaps) });
  return out;
}

function debate(d: Rec): ReportBlock[] {
  const side = str(d.side) === "bull" ? "강세" : str(d.side) === "bear" ? "약세" : str(d.side);
  const t = rec(d.timing);
  const out: ReportBlock[] = [
    {
      title: `${side} 리서처 · 라운드 ${str(d.round) || "0"}`,
      rows: [
        ["입장", label(STANCE_LABEL, d.stance)],
        ["밸류에이션", label(VALUATION_LABEL, d.valuation_view)],
        ["확신", conf(d.conviction)],
        ["타이밍", [label(TIMING_LABEL, t.action), str(t.condition), str(t.price_zone)].filter((x) => x && x !== DASH).join(" · ") || DASH],
      ],
    },
    { title: "논거", list: texts(d.arguments) },
  ];
  if (texts(d.rebuttals).length) out.push({ title: "반박", list: texts(d.rebuttals) });
  if (texts(d.what_would_change_my_mind).length) out.push({ title: "생각을 바꿀 조건", list: texts(d.what_would_change_my_mind) });
  return out;
}

function decision(d: Rec, title: string): ReportBlock[] {
  return [
    {
      title,
      lead: `${termLabel(`action.${str(d.action)}`)} · 목표 비중 ${pct(d.target_weight)} · 확신 ${conf(d.confidence)}`,
      rows: [
        ["주문 방식", str(d.order_type) === "MARKET" ? "시장가" : `지정가 ${price(d.limit_price)}`],
        ["익절가", price(d.take_profit)],
        ["손절가", price(d.stop_loss)],
        ["보유 기간", num(d.horizon_days) === null ? DASH : `${num(d.horizon_days)}일`],
      ],
      paragraphs: str(d.rationale) ? [str(d.rationale)] : undefined,
    },
    ...(texts(d.invalidation_conditions).length ? [{ title: "무효화 조건", list: texts(d.invalidation_conditions) }] : []),
  ];
}

function gate(stage: string, d: Rec): ReportBlock[] {
  const v = rec(d.verdict ?? d);
  const reasons = texts(v.reasons);
  return [
    {
      title: `리스크 게이트 · ${str(v.account_id) || acctOf(stage)}`,
      lead: v.approved ? `승인 ${num(v.adjusted_qty) ?? 0}주` : "거부",
      rows: [
        ["방향", str(v.side) || DASH],
        ["주문", str(v.order_type) === "MARKET" ? "시장가" : v.price ? `지정가 ${price(v.price)}` : DASH],
        ["계좌 모드", str(v.mode) ? termLabel(`mode.${str(v.mode)}`) : DASH],
        ["성향", str(v.stance) ? termLabel(`stance.${str(v.stance)}`) : DASH],
        ["모의 실행", v.dry_run ? "예(주문 없음)" : "아니오"],
      ],
      list: reasons.length ? reasons : undefined,
      paragraphs: str(v.note) ? [str(v.note)] : undefined,
    },
  ];
}

function order(stage: string, d: Rec): ReportBlock[] {
  const req = rec(d.request);
  const res = rec(d.result);
  return [
    {
      title: `주문 · ${acctOf(stage)}`,
      lead: termLabel(`status.${str(d.status)}`) || str(d.status),
      rows: [
        ["요청", `${str(req.side)} ${str(req.qty)}주 ${str(req.order_type) === "MARKET" ? "시장가" : `@${price(req.price)}`}`.trim()],
        ["접수번호", str(res.order_no) || DASH],
        ["모드", str(d.mode) ? termLabel(`mode.${str(d.mode)}`) : DASH],
      ],
      paragraphs: str(res.message) ? [str(res.message)] : undefined,
    },
  ];
}

function inputs(d: Rec): ReportBlock[] {
  const meta = rec(d.meta);
  const q = rec(rec(rec(d.technical).quote).data);
  const rows: [string, string][] = [];
  for (const [key, name] of Object.entries(SECTION_LABEL)) {
    const sec = rec(d[key]);
    rows.push([name, Object.keys(sec).join(", ") || DASH]);
  }
  return [
    {
      title: "수집 입력",
      lead: `${str(meta.name) || str(meta.ticker)} · 현재가 ${price(q.price)} (${pct(q.change_pct, 2)}) · 마지막 봉 ${str(meta.last_candle_date) || DASH}`,
      rows,
      list: texts(d.errors).length ? texts(d.errors) : undefined,
    },
  ];
}

function research(d: Rec): ReportBlock[] {
  const issues = texts(d.issues);
  return [
    {
      title: "토론 요약",
      lead: d.sycophantic ? "토론 붕괴 — 라운드 0 의견을 우선한다" : "정상 수렴",
      rows: [
        ["라운드", str(d.rounds) || DASH],
        ["라운드 0 격차", conf(d.gap_round0)],
        ["최종 격차", conf(d.gap_final)],
        ["타이밍 합의", d.timing_agree === undefined ? DASH : d.timing_agree ? "예" : "아니오"],
        ["우선 의견", str(d.preferred) || DASH],
      ],
      list: issues.length ? issues : undefined,
    },
  ];
}

function quant(d: Rec): ReportBlock[] {
  return [
    {
      title: "퀀트 모델",
      lead: `${label(SIGNAL_LABEL, d.signal)} · 상승 ${pct(d.p_up)} · 보합 ${pct(d.p_flat)} · 하락 ${pct(d.p_down)}`,
      rows: [
        ["모델", str(d.model) || DASH],
        ["기간", num(d.horizon_days) === null ? DASH : `${num(d.horizon_days)}거래일`],
        ["피처 날짜", str(d.feature_date) || DASH],
      ],
    },
  ];
}

function calibration(d: Rec): ReportBlock[] {
  const roles = rec(d.roles);
  return [
    {
      title: "신호 보정",
      lead: d.active ? `활성 · 만기 결정 ${str(d.n_decisions)}건` : `비활성 · 만기 결정 ${str(d.n_decisions)} / ${str(d.min_decisions)}건`,
      rows: Object.entries(roles).map(([k, v]) => [label(ROLE_LABEL, k), `적중 ${pct(rec(v).hit_rate)} (n=${str(rec(v).n)})`]),
    },
  ];
}

function hedgeProposal(d: Rec): ReportBlock[] {
  return [
    {
      title: "전략가 제안",
      lead: `${label(STANCE_LABEL, d.stance)} · 헤지 비중 ${pct(d.hedge_weight)} · 확신 ${conf(d.confidence)}`,
      rows: [
        ["레버리지", d.leveraged ? "예" : "아니오"],
        ["단타 허용", d.scalp_ok === false ? "아니오" : "예"],
        ["기간", num(d.horizon_days) === null ? DASH : `${num(d.horizon_days)}일`],
      ],
      paragraphs: str(d.rationale) ? [str(d.rationale)] : undefined,
      list: texts(d.invalidation_conditions).length ? texts(d.invalidation_conditions) : undefined,
    },
  ];
}

function scan(d: Rec): ReportBlock[] {
  const picks = arr(d.picks).map((p) => (typeof p === "string" ? p : `${str(rec(p).ticker)} — ${str(rec(p).reason)}`));
  return [
    {
      title: "스캔 결과",
      lead: `후보 ${arr(d.candidates).length}개 → picks ${picks.length}개 · 분석 대상 ${arr(d.analyze_targets).length}개${d.cap_hit ? " · 세션 상한" : ""}`,
      list: picks.length ? picks : undefined,
    },
  ];
}

function screen(d: Rec): ReportBlock[] {
  return [
    {
      title: "스크리너 선택",
      list: arr(d.picks).map((p) => `${str(rec(p).ticker)} — ${str(rec(p).reason)}`),
      rows: texts(d.rejected).length ? [["제외", texts(d.rejected).join(", ")]] : undefined,
    },
  ];
}

function discovery(d: Rec): ReportBlock[] {
  return [
    {
      title: "발굴 결과",
      lead: `후보 ${arr(d.candidates).length}개 · 미해결 ${arr(d.unresolved).length}개`,
      list: arr(d.candidates).map((c) => {
        const r = rec(c);
        return `${str(r.name) || str(r.ticker)}${r.ticker ? `(${str(r.ticker)})` : ""} · ${str(r.tier) === "listed" ? "유니버스 밖" : "유니버스"} — ${str(r.catalyst)}`;
      }),
      paragraphs: texts(d.notes).length ? texts(d.notes) : undefined,
    },
  ];
}

/** 모르는 단계: 최상위 값을 표로(목록·객체는 개수만). */
export function genericBlocks(d: Rec, title = "개요"): ReportBlock[] {
  const rows: [string, string][] = Object.entries(d).map(([k, v]) => [
    k,
    Array.isArray(v) ? `${v.length}개 항목` : v && typeof v === "object" ? `항목 ${Object.keys(v as Rec).length}개` : str(v) || DASH,
  ]);
  return rows.length ? [{ title, rows }] : [];
}

export function stageReport(stage: string, data: unknown): ReportBlock[] {
  const d = rec(data);
  if (!Object.keys(d).length) return [];
  if (stage === "00_inputs") return inputs(d);
  if (stage === "05_quant") return quant(d);
  if (stage.startsWith("10_analyst_")) return analyst(d, `${label(ROLE_LABEL, d.role ?? stage.slice(11))} 애널리스트`);
  if (stage.startsWith("20_debate_") || stage.startsWith("_market_debate_")) return debate(d);
  if (stage === "25_research_summary" || stage === "_market_summary") return research(d);
  if (stage === "30_decision") return decision(d, "트레이더 결정");
  if (stage.startsWith("31_decision")) return decision(d, `재평가 결정 ${stage.slice(12)}`);
  if (stage === "35_calibration") return calibration(d);
  if (/^4\d_/.test(stage)) return gate(stage, d);
  if (/^5\d_/.test(stage)) return order(stage, d);
  if (stage === "_market_analyst" || stage.startsWith("_market_analyst_")) return analyst(d, "거시·시장 애널리스트");
  if (stage === "_market_view") return hedgeProposal(d);
  if (stage === "_scan") return scan(d);
  if (stage === "_screen") return screen(d);
  if (/^_discovery(_r\d+)?$/.test(stage)) return discovery(d);
  return genericBlocks(d);
}

const SETTINGS_LABEL: Record<string, string> = {
  markets: "시장",
  risk: "리스크 한도",
  hedge: "헤지·국면",
  models: "모델",
  llm: "LLM 세션",
  web: "웹",
  memory: "메모리",
  discovery: "종목 발굴",
  scalp: "단타",
};

/** 짧은 값 표기: 스칼라 그대로, 스칼라 배열은 쉼표, 객체는 'k=v' 를 이어 붙인다(한 단계). */
function brief(v: unknown): string {
  if (Array.isArray(v)) return v.every((x) => typeof x !== "object") ? v.join(", ") || DASH : `${v.length}개 항목`;
  if (v && typeof v === "object") {
    return Object.entries(v as Rec)
      .map(([k, x]) => `${k}=${x && typeof x === "object" ? (Array.isArray(x) ? x.join("/") : "…") : str(x)}`)
      .join(", ");
  }
  return str(v) || DASH;
}

/** 설정 요약(/api/system/settings): 최상위 절마다 블록, 절 안의 키를 정의 목록으로. */
export function settingsBlocks(data: unknown): ReportBlock[] {
  const d = rec(data);
  const scalars: [string, string][] = [];
  const out: ReportBlock[] = [];
  for (const [k, v] of Object.entries(d)) {
    if (v && typeof v === "object" && !Array.isArray(v)) {
      out.push({ title: SETTINGS_LABEL[k] ? `${SETTINGS_LABEL[k]} (${k})` : k, rows: Object.entries(v as Rec).map(([kk, vv]) => [kk, brief(vv)]) });
    } else {
      scalars.push([k, brief(v)]);
    }
  }
  return scalars.length ? [{ title: "기본", rows: scalars }, ...out] : out;
}
