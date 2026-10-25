// 작동 방식: AI 가 종목을 고르고 토론해 결정하고, 파이썬이 수량·주문을 강제하는 흐름을 한 페이지로.
// 숫자(시간표·한도·모델명)는 API 에서 읽어 설정과 어긋나지 않게 한다.
import { getJson } from "../api";
import { Section } from "../components/Section";
import { Table, Td, Th } from "../components/Table";
import { HOW, type HowSection } from "../how";
import { Link } from "react-router-dom";
import { recentFeatures } from "../nav";
import { useLoad } from "../hooks";
import { jobLabel, scheduleText, type DaemonStatus } from "../jobs";
import type { SystemSettings } from "../types";

function Steps({ s, models }: { s: HowSection; models: Record<string, string> }) {
  return (
    <ol className="how-steps">
      {s.steps.map((st, i) => (
        <li key={i}>
          <div className="how-who">
            <b>{st.who}</b>
            {st.model && models[st.model] ? <small className="muted">{models[st.model]}</small> : null}
          </div>
          <p>{st.what}</p>
        </li>
      ))}
    </ol>
  );
}

export function HowItWorksPage() {
  const settings = useLoad(() => getJson<SystemSettings>("/api/system/settings"), []);
  const daemon = useLoad(() => getJson<DaemonStatus>("/api/system/daemon"), []);
  const models = settings.data?.models ?? {};
  const risk = settings.data?.risk ?? {};
  const pct = (k: string) => (typeof risk[k] === "number" ? `${Math.round((risk[k] as number) * 100)}%` : "—");
  return (
    <>
      <header className="page-h">
        <h1>작동 방식</h1>
        <p className="sub">AI 는 고르고 토론하고 결정한다. 수량·한도·주문은 파이썬이 강제한다. 실전 전환은 사람만 한다.</p>
      </header>
      <Section title="최근 추가" aside="새 것이 위">
        <Table dense>
          <thead>
            <tr>
              <Th>단계</Th>
              <Th>기능</Th>
              <Th>무엇</Th>
            </tr>
          </thead>
          <tbody>
            {recentFeatures(8).map((f) => (
              <tr key={f.title}>
                <Td className="muted">{f.since}</Td>
                <Td>
                  <Link to={f.to}>{f.title}</Link>
                </Td>
                <Td className="wrap">{f.what}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
      </Section>
      <nav className="how-toc" aria-label="절 목록">
        {HOW.map((s) => (
          <a key={s.id} href={`#${s.id}`}>
            {s.title.split(" — ")[0]}
          </a>
        ))}
      </nav>
      <div className="how">
        {HOW.map((s) => (
          <Section key={s.id} title={s.title} className="how-sec">
            <span id={s.id} className="how-anchor" aria-hidden="true" />
            <p className="how-lead">{s.lead}</p>
            {s.id === "day" && (
              <Table dense caption="시간표">
                <thead>
                  <tr>
                    <Th>잡</Th>
                    <Th>언제</Th>
                  </tr>
                </thead>
                <tbody>
                  {(daemon.data?.jobs ?? []).map((j) => (
                    <tr key={j.id}>
                      <Td>{jobLabel(j)}</Td>
                      <Td className="muted">{scheduleText(j)}</Td>
                    </tr>
                  ))}
                  {!daemon.data?.jobs.length && (
                    <tr>
                      <Td colSpan={2} className="muted">
                        시간표는 config/schedule.yaml 에서 읽어요. {daemon.loading ? "불러오는 중…" : "읽지 못했어요."}
                      </Td>
                    </tr>
                  )}
                </tbody>
              </Table>
            )}
            <Steps s={s} models={models} />
            {s.id === "gate" && settings.data && (
              <p className="note">
                현재 설정: 종목당 최대 {pct("max_position_pct")} · 최대 {String(risk.max_positions ?? "—")}종목 · 일일 손실 한도 {pct("daily_loss_stop_pct")} · 기본 손절 {pct("stop_loss_pct")} · 토론 라운드 {settings.data.debate_rounds ?? "—"}회
              </p>
            )}
            {s.rules && (
              <ul className="how-rules">
                {s.rules.map((r, i) => (
                  <li key={i}>{r}</li>
                ))}
              </ul>
            )}
          </Section>
        ))}
      </div>
    </>
  );
}
