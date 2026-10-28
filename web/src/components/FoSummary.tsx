// 대시보드 선물 한 줄 요약(파생 계좌가 있을 때만). 합산에는 섞지 않고 '파생' 화면으로 잇는다.
import { Link } from "react-router-dom";
import { getJson } from "../api";
import { STATE_HINT, stateBadge, summaryParts } from "../fo";
import { useApp, useLoad } from "../hooks";
import type { FoStatus } from "../types";
import { Badge } from "./Badge";

/** 순수 줄(스모크 테스트용). */
export function FoSummaryRows({ accounts }: { accounts: FoStatus[] }) {
  if (!accounts.length) return null;
  return (
    <div className="statusline fo-summary" role="status" aria-label="선물옵션 계좌 요약">
      {accounts.map((s) => {
        const st = stateBadge(s.state);
        return (
          <span key={s.id} className="fo-summary-row">
            <span className="muted">파생 {s.id}</span> <Badge kind="env" value={s.env} />{" "}
            <Badge kind={st.kind} value={st.label} title={STATE_HINT[s.state]} />
            {summaryParts(s).map((p, i) => (
              <span key={i} className={p.tone === "danger" ? "err" : p.tone}>
                {" · "}
                {p.text}
              </span>
            ))}
            {" · "}
            <Link to="/fo">파생 화면으로 ›</Link>
          </span>
        );
      })}
    </div>
  );
}

export function FoSummary() {
  const { foAccounts, tickOf } = useApp();
  const t = tickOf("fo", "control");
  const data = useLoad(() => (foAccounts.length ? getJson<{ accounts: FoStatus[] }>("/api/fo/summary") : null), [foAccounts.length, t]);
  if (!foAccounts.length || !data.data) return null;
  return <FoSummaryRows accounts={data.data.accounts} />;
}
