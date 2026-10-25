/** 결정 추적(Phase 23 E) — /api/decisions/{id}/trace 응답과 행 변환(순수 함수). */

export type TraceAnalyst = { role: string; label: string; signal: string | null; confidence: number | null; summary: string | null; evidence: string[] };
export type TraceDebate = { round: number | null; side: string | null; stance: string | null; conviction: number | null; timing: string | null };
export type TraceGate = { stage: string; account_id: string; approved?: boolean; qty?: number | null; reasons?: string[]; note?: string | null; mode?: string | null; dry_run?: boolean | null };
export type TraceOrder = { stage: string; account_id: string; status?: string | null; order_no?: string | null; message?: string | null; side?: string | null; qty?: number | null; price?: string | null; mode?: string | null };
export type TraceResp = {
  decision: { id: number; date: string; market: string; ticker: string; action: string; data?: Record<string, unknown> };
  quote: string | null;
  analysts: TraceAnalyst[];
  debate: TraceDebate[];
  research: { rounds?: number; gap_round0?: number; gap_final?: number; sycophantic?: boolean; preferred?: string | null } | null;
  gate: TraceGate[];
  orders: TraceOrder[];
  reevaluations: { round: string; decision: Record<string, unknown>; gate: TraceGate[]; orders: TraceOrder[] }[];
  stop_loss: { gate: TraceGate[]; orders: TraceOrder[] };
  reprice: { gate: TraceGate[]; orders: TraceOrder[] };
  stages: string[];
};

const SIGNAL = { BULLISH: "강세", NEUTRAL: "중립", BEARISH: "약세", UNKNOWN: "판단불가" } as Record<string, string>;

export type TraceStep = { key: string; title: string; lines: string[] };

const pct = (v: number | null | undefined) => (v === null || v === undefined ? "—" : v.toFixed(2));

/** 추적 응답 → 단계별 줄 목록(애널리스트 → 토론 → 수렴 → 결정 → 게이트 → 주문 → 재평가·손절·재호가). */
export function traceSteps(t: TraceResp): TraceStep[] {
  const steps: TraceStep[] = [];
  steps.push({
    key: "analysts",
    title: `애널리스트 ${t.analysts.length}`,
    lines: t.analysts.map((a) => `${a.label}: ${SIGNAL[a.signal ?? ""] ?? a.signal ?? "—"} · 확신 ${pct(a.confidence)}${a.evidence.length ? ` · ${a.evidence.join(" / ")}` : ""}`),
  });
  const rounds = [...new Set(t.debate.map((d) => d.round ?? 0))].sort((a, b) => a - b);
  steps.push({
    key: "debate",
    title: `토론 ${rounds.length}라운드`,
    lines: rounds.map((r) => {
      const turns = t.debate.filter((d) => (d.round ?? 0) === r);
      return `r${r}: ${turns.map((d) => `${d.side === "bull" ? "강세" : "약세"} ${SIGNAL[d.stance ?? ""] ?? d.stance ?? "—"} ${pct(d.conviction)}`).join(" · ")}`;
    }),
  });
  if (t.research) {
    steps.push({
      key: "research",
      title: "수렴 감시",
      lines: [t.research.sycophantic ? `붕괴(아첨적 수렴) · 라운드 0 우선 · 차이 ${pct(t.research.gap_round0)}→${pct(t.research.gap_final)}` : `정상 · 차이 ${pct(t.research.gap_round0)}→${pct(t.research.gap_final)}`],
    });
  }
  const d = t.decision.data ?? {};
  steps.push({
    key: "decision",
    title: `결정 ${t.decision.action}`,
    lines: [`비중 ${pct(d.target_weight as number | undefined)} · 확신 ${pct(d.confidence as number | undefined)} · 보유 ${String(d.horizon_days ?? "—")}일${t.quote ? ` · 당시 현재가 ${t.quote}` : ""}`],
  });
  steps.push({ key: "gate", title: `게이트 ${t.gate.length}계좌`, lines: t.gate.map(gateLine) });
  steps.push({ key: "orders", title: `주문 ${t.orders.length}`, lines: t.orders.map(orderLine) });
  for (const r of t.reevaluations) {
    steps.push({ key: `re-${r.round}`, title: `재평가 ${r.round}: ${String(r.decision.action ?? "—")}`, lines: [...r.gate.map(gateLine), ...r.orders.map(orderLine)] });
  }
  if (t.stop_loss.gate.length || t.stop_loss.orders.length) {
    steps.push({ key: "stop", title: "손절", lines: [...t.stop_loss.gate.map(gateLine), ...t.stop_loss.orders.map(orderLine)] });
  }
  if (t.reprice.gate.length || t.reprice.orders.length) {
    steps.push({ key: "reprice", title: "재호가", lines: [...t.reprice.gate.map(gateLine), ...t.reprice.orders.map(orderLine)] });
  }
  return steps;
}

export function gateLine(g: TraceGate): string {
  const head = g.approved ? `승인 ${g.qty ?? 0}주` : "거부";
  const why = (g.reasons ?? []).join("; ");
  return `${g.account_id}: ${head}${why ? ` · ${why}` : ""}${g.note ? ` (${g.note})` : ""}${g.dry_run ? " · dry-run" : ""}`;
}

export function orderLine(o: TraceOrder): string {
  return `${o.account_id}: ${o.side ?? ""} ${o.qty ?? ""}주${o.price ? ` @${o.price}` : " 시장가"} → ${o.status ?? "—"}${o.order_no ? ` #${o.order_no}` : ""}${o.message ? ` · ${o.message}` : ""}`;
}

/** 지정가까지 거리(%): 당시 현재가 대비 지정가. 시장가·현재가 없음은 null. */
export function limitDistancePct(price: string | null | undefined, quote: string | null | undefined): number | null {
  const p = price ? Number(price) : NaN;
  const q = quote ? Number(quote) : NaN;
  if (!Number.isFinite(p) || !Number.isFinite(q) || q <= 0) return null;
  return ((p - q) / q) * 100;
}

/** unknown 주문 경고줄 문구. 없으면 "". */
export function unknownLine(orders: { status: string }[]): string {
  const n = orders.filter((o) => o.status === "unknown").length;
  return n ? `확인 필요 ${n}건 — 응답을 받지 못한 주문(unknown). 체결 동기화가 확정하지 못하면 KIS 앱에서 확인하세요.` : "";
}
