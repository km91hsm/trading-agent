// 계좌 추가·삭제(config/accounts.yaml). 토큰 + 이 PC 에서 직접 연 화면에서만 동작(서버가 강제).
// 계좌번호는 여기서 받지 않는다 — 추가 뒤 재시작하면 '연결 키'에 그 계좌 칸(KIS_<ID>_*)이 생긴다.
import { useState } from "react";
import { postJson } from "../api";
import { credPrefix, draftPayload, emptyDraft, removeConfirmWord, validateDraft, type AccountDraft } from "../accountsForm";
import type { Account, Market } from "../types";
import { Badge } from "./Badge";
import { ConfirmDialog } from "./ConfirmDialog";
import { Section } from "./Section";
import { Table, Td, Th } from "./Table";
import { useToast } from "./Toast";

export function AccountsPanel({ token, accounts }: { token: string | null; accounts: Account[] }) {
  const toast = useToast();
  const [draft, setDraft] = useState<AccountDraft>(emptyDraft());
  const [busy, setBusy] = useState(false);
  const [changed, setChanged] = useState<string[]>([]);
  const [removing, setRemoving] = useState<Account | null>(null);
  const [confirmText, setConfirmText] = useState("");
  const problem = draft.id ? validateDraft(draft, accounts) : null;
  const confirmWord = removing ? removeConfirmWord(removing) : null;

  const setMarket = (m: Market, on: boolean) =>
    setDraft((d) => ({ ...d, markets: on ? [...d.markets.filter((x) => x !== m), m] : d.markets.filter((x) => x !== m) }));

  const add = async () => {
    const err = validateDraft(draft, accounts);
    if (err) {
      toast(err, "error");
      return;
    }
    const body = draftPayload(draft);
    setBusy(true);
    try {
      await postJson("/api/control/accounts", body, token);
      toast(`계좌 ${body.id} 를 추가했어요 — 앱을 재시작하면 연결 키에 이 계좌 칸이 생겨요`, "ok");
      setChanged((c) => [...c, `추가 ${body.id}`]);
      setDraft(emptyDraft());
    } catch (e) {
      toast(`실패: ${(e as Error).message}`, "error");
    } finally {
      setBusy(false);
    }
  };

  const remove = async (a: Account) => {
    setBusy(true);
    try {
      await postJson("/api/control/accounts/remove", { account_id: a.id }, token);
      toast(`계좌 ${a.id} 를 목록에서 뺐어요 — 재시작 뒤 적용(원장 기록은 그대로)`, "ok");
      setChanged((c) => [...c, `삭제 ${a.id}`]);
    } catch (e) {
      toast(`실패: ${(e as Error).message}`, "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Section title="계좌" aside={changed.length ? <Badge kind="pending" value="재시작해야 적용" /> : "config/accounts.yaml"}>
      <p className="note">
        같은 명의로 실전·모의 계좌를 여럿 쓸 수 있다. 여기서 계좌를 추가하면 계좌번호 자리는 비워 두고(자리표시자), 앱을 재시작하면 아래 '연결 키'에 그 계좌의 앱키·계좌번호 칸이 생긴다. 실전
        계좌는 off 로 시작한다. 삭제는 살아 있는 주문·승인 요청·보유 종목·열린 파생 포지션이 없을 때만 되고, 원장 기록은 지우지 않는다. 이 PC 에서 직접 연 화면에서만 바뀐다.
      </p>
      {!token && <p className="note">제어 토큰이 없어 바꿀 수 없다 — 위 '제어 토큰'을 먼저 저장하세요.</p>}
      {changed.length > 0 && <p className="note">이번에 바꾼 것: {changed.join(" · ")} — 앱(데몬·웹)을 재시작하면 목록과 연결 키에 반영된다.</p>}
      <Table dense caption="등록된 계좌">
        <thead>
          <tr>
            <Th>계좌</Th>
            <Th>종류</Th>
            <Th>시장</Th>
            <Th>모드</Th>
            <Th>삭제</Th>
          </tr>
        </thead>
        <tbody>
          {accounts.map((a) => (
            <tr key={a.id}>
              <Td>
                <Badge kind="env" value={a.env} /> {a.id}
              </Td>
              <Td>{a.asset === "fo" ? "선물옵션" : "현물"}</Td>
              <Td>{a.markets.join(", ")}</Td>
              <Td>
                <Badge kind="mode" value={a.mode} />
              </Td>
              <Td>
                <button
                  type="button"
                  className="btn btn-sm btn-danger"
                  disabled={!token || busy}
                  onClick={() => {
                    setConfirmText("");
                    setRemoving(a);
                  }}
                >
                  삭제
                </button>
              </Td>
            </tr>
          ))}
        </tbody>
      </Table>
      <h3 className="sub-h">계좌 추가</h3>
      <form
        className="row"
        onSubmit={(e) => {
          e.preventDefault();
          void add();
        }}
      >
        <label className="field">
          <span>계좌 id (예: real-2)</span>
          <input type="text" value={draft.id} onChange={(e) => setDraft((d) => ({ ...d, id: e.target.value }))} size={14} spellCheck={false} />
        </label>
        <label className="field">
          <span>환경</span>
          <select value={draft.env} onChange={(e) => setDraft((d) => ({ ...d, env: e.target.value as AccountDraft["env"] }))}>
            <option value="real">실전 · off 로 시작</option>
            <option value="paper">모의 · auto</option>
          </select>
        </label>
        <label className="field">
          <span>종류</span>
          <select value={draft.asset} onChange={(e) => setDraft((d) => ({ ...d, asset: e.target.value as AccountDraft["asset"] }))}>
            <option value="stock">현물(상품코드 01)</option>
            <option value="fo">선물옵션(상품코드 03 · KR)</option>
          </select>
        </label>
        {draft.asset === "stock" && (
          <div className="field">
            <span>시장</span>
            <div className="row tight">
              {(["KR", "US"] as Market[]).map((m) => (
                <label className="row tight" key={m}>
                  <input type="checkbox" checked={draft.markets.includes(m)} onChange={(e) => setMarket(m, e.target.checked)} /> {m}
                </label>
              ))}
            </div>
          </div>
        )}
        <button type="submit" className="btn btn-primary" disabled={!token || busy || !draft.id.trim() || !!problem}>
          추가
        </button>
      </form>
      {problem ? (
        <p className="note">{problem}</p>
      ) : draft.id.trim() ? (
        <p className="note">
          재시작 뒤 연결 키에 <code>{credPrefix(draft.id.trim())}APP_KEY</code>·<code>…APP_SECRET</code>·<code>…CANO</code> 칸이 생긴다.
        </p>
      ) : null}
      <ConfirmDialog
        open={removing !== null}
        title={`계좌 ${removing?.id ?? ""} 를 목록에서 뺍니다`}
        body={
          <>
            <p>accounts.yaml 에서 이 계좌 줄을 지운다(바꾸기 전 백업). 주문·잔고 기록과 연결 키는 그대로 남고, 재시작 뒤 이 계좌는 거래·조회 대상에서 빠진다.</p>
            {confirmWord && (
              <label className="field">
                <span>실전 계좌 — 확인으로 계좌 id 「{confirmWord}」를 입력</span>
                <input type="text" value={confirmText} onChange={(e) => setConfirmText(e.target.value)} size={14} spellCheck={false} />
              </label>
            )}
          </>
        }
        confirmLabel="삭제"
        danger
        onConfirm={() => {
          const a = removing;
          if (!a || (confirmWord && confirmText !== confirmWord)) {
            toast(`확인 문자열 「${confirmWord}」 이 맞지 않아요`, "error");
            return;
          }
          setRemoving(null);
          void remove(a);
        }}
        onClose={() => setRemoving(null)}
      />
    </Section>
  );
}
