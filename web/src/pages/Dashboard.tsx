import { getJson } from "../api";
import { ErrorBox } from "../components/Tables";
import { useApp, useLoad } from "../hooks";
import { stanceText } from "../stance";
import type { Decision } from "../types";

type Calls = { calls: Array<{ cost_usd: number | null }>; sessions_today: number; cost_today: number };

export function Dashboard() {
  const { accounts, events, tick } = useApp();
  const decisions = useLoad(() => getJson<Decision[]>("/api/decisions", { days: "30" }), [tick]);
  const calls = useLoad(() => getJson<Calls>("/api/calls", { days: "1" }), [tick]);
  const dist: Record<string, number> = {};
  for (const d of decisions.data ?? []) dist[d.action] = (dist[d.action] ?? 0) + 1;
  return (
    <section>
      <h1>대시보드</h1>
      {events?.paused && <div className="error">일시정지 상태입니다 (설정/제어에서 재개).</div>}
      <div className="cards">
        {accounts.map((a) => (
          <div key={a.id} className={`card ${a.env}`}>
            <h3>
              {a.id} <small>{a.env === "real" ? "실전" : "모의"}</small>
            </h3>
            <div>모드: <b>{a.mode}</b> · 성향: {stanceText(a)}</div>
            <div>NAV KR: {a.nav_by_market.KR ?? "-"} · US: {a.nav_by_market.US ?? "-"}</div>
            <div>미승인 주문: {a.pending_approvals}</div>
          </div>
        ))}
      </div>
      <h2>결정 분포 (30일)</h2>
      <ErrorBox error={decisions.error} />
      {Object.keys(dist).length ? (
        <ul className="inline">
          {Object.entries(dist).map(([k, v]) => (
            <li key={k}>
              {k} <b>{v}</b>
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">결정 없음</p>
      )}
      <h2>오늘 LLM 호출</h2>
      <ErrorBox error={calls.error} />
      <p>
        세션 {calls.data?.sessions_today ?? 0}건 · 비용 ${(calls.data?.cost_today ?? 0).toFixed(2)}
      </p>
    </section>
  );
}
