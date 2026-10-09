import { useState } from "react";
import { getJson, postJson, withAccount } from "../api";
import { AccountPicker } from "../components/AccountPicker";
import { ApprovalsTable, ErrorBox, OrdersTable, PositionsTable } from "../components/Tables";
import { useApp, useLoad } from "../hooks";
import type { Market, Portfolio } from "../types";

export function AccountPage() {
  const { accountId, accounts, token, tick } = useApp();
  const acct = accounts.find((a) => a.id === accountId);
  const [market, setMarket] = useState<Market>("KR");
  const [msg, setMsg] = useState<string | null>(null);
  const pf = useLoad(
    () => (accountId ? getJson<Portfolio>(withAccount(`/api/portfolio?market=${market}`, accountId)) : null),
    [accountId, market, tick],
  );
  const refresh = async () => {
    try {
      await postJson(withAccount(`/api/portfolio/refresh?market=${market}`, accountId), null, token);
      setMsg("잔고를 새로 읽었습니다");
      pf.reload();
    } catch (e) {
      setMsg(`새로고침 실패: ${(e as Error).message}`);
    }
  };
  return (
    <section>
      <h1>계좌 상세</h1>
      <div className="row">
        <AccountPicker />
        <label>
          시장
          <select value={market} onChange={(e) => setMarket(e.target.value as Market)}>
            {(acct?.markets ?? ["KR"]).map((m) => (
              <option key={m}>{m}</option>
            ))}
          </select>
        </label>
        <button onClick={refresh} disabled={!token} title={token ? "" : "토큰이 필요합니다"}>
          잔고 새로고침
        </button>
        {msg && <span className="muted">{msg}</span>}
      </div>
      <ErrorBox error={pf.error} />
      {acct && (
        <p>
          {acct.env === "real" ? "실전" : "모의"} · 모드 <b>{acct.mode}</b> · 성향 {acct.stance}
        </p>
      )}
      <h2>포지션 {pf.data?.snapshot ? `(스냅샷 ${pf.data.snapshot.date})` : ""}</h2>
      {pf.data?.snapshot ? (
        <>
          <p>
            NAV {pf.data.snapshot.nav} · 현금 {pf.data.snapshot.cash}
          </p>
          <PositionsTable rows={pf.data.snapshot.positions} />
        </>
      ) : (
        <p className="muted">스냅샷 없음 (잔고 새로고침 또는 분석 실행 후 생성)</p>
      )}
      <h2>미승인 주문</h2>
      <ApprovalsTable rows={pf.data?.approvals ?? []} />
      <h2>최근 주문</h2>
      <OrdersTable rows={pf.data?.orders ?? []} />
    </section>
  );
}
