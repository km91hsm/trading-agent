// 데몬 잡 표: 잡 · 주기 · 마지막 실행 · 결과 · 다음 예정. 데몬이 없어도 예정표는 보인다.
import { fmtDate, fmtTime } from "../format";
import { useApp } from "../hooks";
import { JOB_STATE_LABEL, canRun, jobLabel, jobState, runNowRequest, scheduleText, type DaemonStatus } from "../jobs";
import { EmptyState } from "./EmptyState";
import { RunJobButton } from "./RunJob";
import { Table, Td, Th } from "./Table";

function when(iso: string | null, now: Date): string {
  if (!iso) return "—";
  const sameDay = fmtDate(iso) === fmtDate(now.toISOString());
  return sameDay ? fmtTime(iso, "KR") : `${fmtDate(iso).slice(5)} ${fmtTime(iso, "KR")}`;
}

export function JobsBlock({ data, now }: { data: DaemonStatus | null; now: Date }) {
  const { token } = useApp();
  if (!data) return <p className="note">불러오는 중…</p>;
  const run = canRun(data, token);
  const rows = data.jobs.filter((j) => j.kind !== "job_queue"); // 대기열 실행기는 아래 대기열 블록이 대신 보여 준다
  if (!data.jobs.length) return <EmptyState title="잡 예정표가 없어요" hint="config/schedule.yaml 을 읽지 못했어요. 데몬 로그를 보세요." />;
  return (
    <>
      {data.pid_mismatch && <p className="note err">하트비트 파일의 pid 가 잠금과 달라요. 옛 데몬의 기록일 수 있어요.</p>}
      {!data.alive && <p className="note">데몬이 꺼져 있어요. `trader daemon` 또는 Trader.app 으로 켜면 아래 표가 채워져요.</p>}
      <Table dense caption="데몬 잡">
        <thead>
          <tr>
            <Th>잡</Th>
            <Th>주기</Th>
            <Th>마지막 실행</Th>
            <Th>결과</Th>
            <Th>다음 예정</Th>
            <Th>지금 실행</Th>
          </tr>
        </thead>
        <tbody>
          {rows.map((j) => {
            const st = jobState(j);
            const spec = runNowRequest(j);
            return (
              <tr key={j.id} data-state={st}>
                <Td>
                  <span className="nm">
                    {jobLabel(j)}
                    <small>{j.id}</small>
                  </span>
                </Td>
                <Td className="muted">{scheduleText(j)}</Td>
                <Td>{when(j.last_end ?? j.last_start, now)}</Td>
                <Td className={st === "error" || st === "missed" ? "gate no" : st === "ok" ? "gate ok" : "muted"}>
                  <span title={j.last_error ?? undefined}>
                    {JOB_STATE_LABEL[st]}
                    {j.last_error ? <small className="muted"> · {j.last_error.slice(0, 40)}</small> : null}
                  </span>
                </Td>
                <Td className="muted">{data.alive ? when(j.next_run, now) : "—"}</Td>
                <Td>
                  {spec ? (
                    <RunJobButton
                      spec={spec}
                      small
                      ariaLabel={`${jobLabel(j)} 지금 실행`}
                      disabledReason={run.reason}
                      confirm={
                        j.kind === "scan_analyze"
                          ? {
                              title: `${jobLabel(j)} 을 지금 실행할까요?`,
                              danger: true,
                              body: (
                                <>
                                  <p>Claude 세션이 분석 대상 종목 수만큼 뜨고, 데몬이 dry-run 이 아니면 auto 계좌에 주문이 나갈 수 있어요.</p>
                                  <p>다른 예약 잡(스캔·복기·학습)은 이 작업이 끝날 때까지 기다려요. 주말·휴장일 확인은 하지 않아요.</p>
                                </>
                              ),
                            }
                          : undefined
                      }
                    />
                  ) : null}
                </Td>
              </tr>
            );
          })}
        </tbody>
      </Table>
      {!run.ok && <p className="note">지금 실행: {run.reason}</p>}
    </>
  );
}
