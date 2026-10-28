// 작업 대기열 표: 대기·실행 중 먼저, 그 뒤 최근 종료분. 활성 요청이 있을 때만 리더 탭이 3초 폴링하고
// 다른 탭에 중계한다 (진행 문구는 SSE 에 없다).
import { useEffect, useState } from "react";
import { getJson, isEventsLeader, postJson } from "../api";
import { useApp, useLoad } from "../hooks";
import { fmtDate, fmtTime } from "../format";
import { JOBS_CHANNEL, JOBS_POLL_MS, pollsJobs, requestRows, type JobsResp, type RequestRowView } from "../jobs";
import { Badge } from "./Badge";
import { EmptyState } from "./EmptyState";
import { Table, Td, Th } from "./Table";
import { useToast } from "./Toast";

function when(iso: string | null, now: Date): string {
  if (!iso) return "—";
  const sameDay = fmtDate(iso) === fmtDate(now.toISOString());
  return sameDay ? fmtTime(iso, "KR") : `${fmtDate(iso).slice(5)} ${fmtTime(iso, "KR")}`;
}

const BADGE: Record<RequestRowView["state"], "neutral" | "pending" | "ok"> = {
  queued: "neutral",
  running: "pending",
  ok: "ok",
  error: "neutral",
  skipped: "neutral",
};

const LONG_DETAIL = 60; // 이보다 긴 진행·사유는 두 줄로 접고 '더 보기' 로 펼친다

/** 진행·사유: 말줄임으로 자르지 않는다(마우스를 못 올리는 모바일에서도 끝까지 읽게). */
function Detail({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  const long = text.length > LONG_DETAIL;
  return (
    <span className="progress-cell">
      <span className={`progress-text${long && !open ? " clamp" : ""}`}>{text}</span>
      {long ? (
        <button type="button" className="btn btn-sm btn-ghost" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
          {open ? "접기" : "더 보기"}
        </button>
      ) : null}
    </span>
  );
}

/** 순수 표 (스모크 테스트용). onCancel 이 없으면 취소 열을 비활성으로 그린다. */
export function JobRequestsTable({ rows, now, onCancel }: { rows: RequestRowView[]; now: Date; onCancel?: (id: number) => void }) {
  if (!rows.length) return <EmptyState title="대기·실행 중인 작업이 없어요" hint="잡 표의 '지금 실행' 이나 데이터·ML 페이지의 버튼이 여기에 요청을 넣어요." />;
  return (
    <Table dense caption="작업 대기열">
      <thead>
        <tr>
          <Th>작업</Th>
          <Th>상태</Th>
          <Th num>시각</Th>
          <Th>진행 · 사유</Th>
          <Th>동작</Th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.key} data-state={r.state}>
            <Td>
              <span className="nm">
                {r.label}
                <small>#{r.id} · {r.kind}</small>
              </span>
            </Td>
            <Td className={r.state === "error" ? "gate no" : r.state === "ok" ? "gate ok" : undefined}>
              <Badge kind={BADGE[r.state]} value={r.stateLabel} />
              {r.priority && r.state === "queued" ? <Badge kind="pending" value="우선" title="우선순위가 높아 먼저 실행돼요" /> : null}
            </Td>
            <Td num className="muted">
              {when(r.when, now)}
            </Td>
            <Td>{r.detail ? <Detail text={r.detail} /> : <span className="muted">—</span>}</Td>
            <Td>
              {r.cancellable ? (
                <button type="button" className="btn btn-sm btn-ghost" disabled={!onCancel} title={onCancel ? undefined : "토큰이 필요해요"} onClick={() => onCancel?.(r.id)}>
                  취소
                </button>
              ) : null}
            </Td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}

export function JobRequestsBlock({ now }: { now: Date }) {
  const { tick, token, events } = useApp();
  const toast = useToast();
  const jobs = useLoad(() => getJson<JobsResp>("/api/jobs?limit=20"), [tick]);
  // 진행 문구는 SSE 에 없어 활성 요청이 있는 동안 폴링한다 — 리더 탭만 부르고 응답을 다른 탭에 중계
  const [relayed, setRelayed] = useState<JobsResp | null>(null);
  useEffect(() => setRelayed(null), [jobs.data]); // 새로 읽은 값이 오면 그것이 최신
  const view = relayed ?? jobs.data;
  const active = (events?.jobs_active ?? view?.active ?? 0) > 0;
  useEffect(() => {
    let ch: BroadcastChannel | null = null;
    try {
      ch = typeof BroadcastChannel !== "undefined" ? new BroadcastChannel(JOBS_CHANNEL) : null;
    } catch {
      ch = null;
    }
    if (ch) ch.onmessage = (e: MessageEvent) => setRelayed(e.data as JobsResp);
    if (!active) return () => ch?.close();
    const h = setInterval(() => {
      if (!pollsJobs(active, isEventsLeader(), document.visibilityState === "visible")) return;
      getJson<JobsResp>("/api/jobs?limit=20")
        .then((d) => {
          setRelayed(d);
          ch?.postMessage(d);
        })
        .catch(() => undefined);
    }, JOBS_POLL_MS);
    return () => {
      clearInterval(h);
      ch?.close();
    };
  }, [active]);
  const cancel = async (id: number) => {
    try {
      await postJson("/api/control/jobs/cancel", { id }, token);
      toast(`요청 #${id} 를 취소했어요`, "ok");
      jobs.reload();
    } catch (e) {
      toast(`실패: ${(e as Error).message}`, "error");
    }
  };
  if (!view) return <p className="note">불러오는 중…</p>;
  return (
    <>
      {!view.alive && view.active > 0 && <p className="note">데몬이 꺼져 있어요. 켜면 대기 요청을 순서대로 실행해요.</p>}
      <JobRequestsTable rows={requestRows(view.requests)} now={now} onCancel={token ? cancel : undefined} />
    </>
  );
}
