// 시스템 현황: 대시보드에서 뺀 운영 블록(Claude 사용량·모델·작업 대기열·데몬 잡)을 한 화면에.
// 대시보드는 시세·보유·주문에 집중하고, 상태줄의 데몬·Claude 칩이 이 화면으로 이어진다.
import { getJson } from "../api";
import { JobRequestsBlock } from "../components/JobRequests";
import { JobsBlock } from "../components/Jobs";
import { Section } from "../components/Section";
import { Table, Td } from "../components/Table";
import { Term } from "../components/Term";
import { UsageBlock } from "../components/Usage";
import { useApp, useLoad, useNow } from "../hooks";
import { daemonText, type DaemonStatus } from "../jobs";
import type { UsageResp } from "../usage";
import type { CallsResp, SystemSettings } from "../types";

type ModelsResp = { active: { version: string; metrics: { alpha: number | null; accuracy: number | null } } | null };
type RlResp = { active: { version: string } | null; experiments: Array<{ version: string }> };

export function SystemPage() {
  const { tickOf } = useApp();
  const tRuns = tickOf("runs");
  const tUsage = tickOf("usage");
  const tDaemon = tickOf("daemon");
  const tControl = tickOf("control");
  const tJobs = tickOf("jobs_done", "runs");
  const now = useNow(30_000);
  const minute = Math.floor(now.getTime() / 60_000);
  const usage = useLoad(() => getJson<UsageResp>("/api/system/usage"), [tRuns, tUsage, minute]);
  const daemon = useLoad(() => getJson<DaemonStatus>("/api/system/daemon"), [tDaemon, tRuns, minute]);
  const calls = useLoad(() => getJson<CallsResp>("/api/calls", { days: "1" }), [tRuns, tUsage]);
  const settings = useLoad(() => getJson<SystemSettings>("/api/system/settings"), [tControl]);
  const models = useLoad(() => getJson<ModelsResp>("/api/ml/models"), [tJobs]);
  const rl = useLoad(() => getJson<RlResp>("/api/rl/experiments"), [tJobs]);
  const signal = daemonText(daemon.data);
  const cap = settings.data?.llm?.daily_session_cap;

  return (
    <>
      <header className="page-h">
        <h1>시스템 현황</h1>
        <p className="sub">Claude 사용량·모델·작업 대기열·데몬 잡 · 새로고침 없이 실시간</p>
      </header>
      <div className="statusline" role="status">
        <span className={signal.level === "none" ? "" : signal.level} title="daemon.lock 의 pid 생존 + 하트비트 파일(잡마다 갱신)">
          데몬 <b>{signal.text}</b>
          {daemon.data?.pid ? <span className="muted"> pid {daemon.data.pid}</span> : null}
        </span>
        <span>
          오늘 세션 <b>{calls.data?.sessions_today ?? 0}{cap ? ` / ${cap}` : ""}</b> · ${(calls.data?.cost_today ?? 0).toFixed(2)}
        </span>
      </div>
      <div className="cols-2">
        <div>
          <Section title="Claude 사용량" aside="구독 창 · 세션">
            <UsageBlock data={usage.data} />
          </Section>
          <Section title="모델" aside="승격 상태">
            <Table dense caption="모델 승격 상태">
              <tbody>
                <tr>
                  <Td>quant</Td>
                  <Td num>
                    {models.data?.active ? (
                      <>
                        {models.data.active.version} · α {models.data.active.metrics.alpha ?? "—"}
                      </>
                    ) : (
                      <span className="muted">미승격 · ML 없이 운영</span>
                    )}
                  </Td>
                </tr>
                <tr>
                  <Td>RL 사이저</Td>
                  <Td num>{rl.data?.active ? rl.data.active.version : <span className="muted">미승격 · 섀도</span>}</Td>
                </tr>
                <tr>
                  <Td>
                    <Term k="calibration" />
                  </Td>
                  <Td num>
                    <span className="muted">만기 결정 60건부터</span>
                  </Td>
                </tr>
              </tbody>
            </Table>
          </Section>
          <Section title="작업 대기열" aside="지금 실행 요청 · 데몬이 순서대로">
            <JobRequestsBlock now={now} />
          </Section>
        </div>
        <div>
          <Section title="데몬 잡" aside={daemon.data ? `${daemon.data.jobs.length}개 · 실시간` : undefined}>
            <JobsBlock data={daemon.data} now={now} />
          </Section>
        </div>
      </div>
    </>
  );
}
