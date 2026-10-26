import { useApp } from "../hooks";
import { fmtMoney } from "../format";
import { Badge } from "./Badge";

/** '전체' 선택 상태에서 계좌 단위 화면을 열면 계좌를 고르게 한다 (Phase 24). */
export function AccountPicker({ purpose }: { purpose: string }) {
  const { accounts, setAccountId } = useApp();
  return (
    <div className="picker">
      <p className="muted">{purpose} 화면은 계좌마다 따로 봐요. 볼 계좌를 고르세요 — 모든 계좌 합산은 대시보드에 있어요.</p>
      <ul className="picker-list">
        {accounts.map((a) => (
          <li key={a.id}>
            <button type="button" className="btn picker-card" onClick={() => setAccountId(a.id)}>
              <span className="picker-id">
                {a.id} <Badge kind="env" value={a.env} /> <Badge kind="mode" value={a.mode} />
              </span>
              <span className="muted">
                {a.markets.map((m) => `${m} ${fmtMoney(a.nav_by_market[m] ?? null, m)}`).join(" · ")}
                {a.pending_approvals ? ` · 미승인 ${a.pending_approvals}` : ""}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
