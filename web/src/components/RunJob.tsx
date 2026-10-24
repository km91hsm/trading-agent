// '지금 실행' 버튼: 작업 대기열에 요청을 넣는다. 모든 페이지가 이것만 쓴다 (디자인 문서 docs/design/2026-10-24-phase17-ui-patterns.md 패턴 1).
import { useState, type ReactNode } from "react";
import { getJson, postJson } from "../api";
import { useApp, useLoad } from "../hooks";
import { canRun, type JobsResp } from "../jobs";
import { ConfirmDialog } from "./ConfirmDialog";
import { useToast } from "./Toast";

export type JobSpec = { kind: string; params: Record<string, unknown> };

/** 버튼 활성 여부(토큰 + 데몬 생존). 데몬 생존은 /api/jobs 의 alive 로 본다. */
export function useCanRun(): { ok: boolean; reason?: string } {
  const { token, tick } = useApp();
  const jobs = useLoad(() => getJson<JobsResp>("/api/jobs?limit=1"), [tick]);
  return canRun(jobs.data ? { alive: jobs.data.alive } : null, token);
}

/** 요청 1건을 POST 하고 토스트로 알린다. 성공하면 요청 id, 실패하면 null. */
export function useRunJob() {
  const { token } = useApp();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const run = async (spec: JobSpec, okMsg?: string): Promise<number | null> => {
    setBusy(true);
    try {
      const r = await postJson<{ id: number; label: string }>("/api/control/jobs", spec, token);
      toast(okMsg ?? `대기열에 넣었어요: ${r.label}`, "ok");
      return r.id;
    } catch (e) {
      toast(`실패: ${(e as Error).message}`, "error");
      return null;
    } finally {
      setBusy(false);
    }
  };
  return { run, busy };
}

export function RunJobButton({
  spec,
  label = "지금 실행",
  ariaLabel,
  disabledReason,
  confirm,
  extra,
  small,
  primary,
  onDone,
}: {
  spec: JobSpec | (() => JobSpec | JobSpec[]);
  label?: string;
  ariaLabel?: string;
  disabledReason?: string;
  confirm?: { title: string; body?: ReactNode; danger?: boolean; confirmLabel?: string };
  extra?: () => JobSpec[]; // 확인 뒤 이어서 넣을 요청(예: 백필 뒤 학습)
  small?: boolean;
  primary?: boolean;
  onDone?: () => void;
}) {
  const { run, busy } = useRunJob();
  const [ask, setAsk] = useState(false);
  const specs = (): JobSpec[] => {
    const s = typeof spec === "function" ? spec() : spec;
    return Array.isArray(s) ? s : [s];
  };
  const go = async () => {
    setAsk(false);
    for (const s of [...specs(), ...(extra ? extra() : [])]) {
      const id = await run(s);
      if (id == null) break;
    }
    onDone?.();
  };
  const cls = ["btn", small ? "btn-sm" : "", primary ? "btn-primary" : ""].filter(Boolean).join(" ");
  return (
    <>
      <button
        type="button"
        className={cls}
        disabled={!!disabledReason || busy}
        title={disabledReason}
        aria-label={ariaLabel}
        onClick={() => (confirm ? setAsk(true) : go())}
      >
        {busy ? "요청 중…" : label}
      </button>
      {confirm && (
        <ConfirmDialog
          open={ask}
          title={confirm.title}
          body={confirm.body}
          confirmLabel={confirm.confirmLabel ?? "실행"}
          danger={confirm.danger}
          onConfirm={go}
          onClose={() => setAsk(false)}
        />
      )}
    </>
  );
}
