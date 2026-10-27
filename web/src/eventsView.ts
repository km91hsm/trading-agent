// 사건 수혜(2026-10-27) 화면용 순수 함수.

export type EventTicker = { ticker: string; name: string; role?: string; exchange?: string };
export type DetectedEvent = { id: string; label: string; kind: "shock" | "structural"; hits: number; news: Array<{ id: number; title: string }>; tickers: EventTicker[] };
export type EventsResp = { enabled: boolean; market: string; date: string | null; events: DetectedEvent[]; table: Array<{ id: string; label: string; kind: string; n: number }> };

export function kindLabel(kind: string): string {
  return kind === "shock" ? "단기 사건" : "구조적 흐름";
}

/** 수혜 종목을 역할별로 묶는다 — '방산: 한화에어로스페이스, 현대로템 · 금: …'. */
export function rolesLine(tickers: EventTicker[]): string {
  const by = new Map<string, string[]>();
  for (const t of tickers) {
    const r = t.role || "기타";
    by.set(r, [...(by.get(r) ?? []), t.name]);
  }
  return [...by.entries()].map(([r, ns]) => `${r}: ${ns.join(", ")}`).join(" · ");
}

/** 오늘이 아닌 감지 결과는 보이지 않는다. */
export function todayEvents(resp: EventsResp | null, today: string): DetectedEvent[] {
  if (!resp || resp.date !== today) return [];
  return resp.events;
}
