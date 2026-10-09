import { useState } from "react";
import { getJson, postJson } from "../api";
import { JsonView } from "../components/JsonView";
import { ErrorBox } from "../components/Tables";
import { useApp, useLoad } from "../hooks";

export function SettingsPage() {
  const { accounts, token, setToken, events, reloadAccounts, tick } = useApp();
  const [tokenInput, setTokenInput] = useState(token ?? "");
  const [acct, setAcct] = useState("");
  const [mode, setMode] = useState("off");
  const [confirm, setConfirm] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const settings = useLoad(() => getJson<Record<string, unknown>>("/api/system/settings"), [tick]);
  const status = useLoad(() => getJson<{ markdown: string }>("/api/system/status"), [tick]);
  const call = async (path: string, body: unknown, ok: string) => {
    try {
      await postJson(path, body, token);
      setMsg(ok);
      reloadAccounts();
    } catch (e) {
      setMsg(`실패: ${(e as Error).message}`);
    }
  };
  const target = accounts.find((a) => a.id === (acct || accounts[0]?.id));
  return (
    <section>
      <h1>설정/제어</h1>
      <h2>제어 토큰</h2>
      <div className="row">
        <input type="password" placeholder="WEB_TOKEN" value={tokenInput} onChange={(e) => setTokenInput(e.target.value)} />
        <button onClick={() => { setToken(tokenInput.trim()); setMsg("토큰을 저장했습니다 (이 브라우저에만)"); }}>저장</button>
        <button onClick={() => { setTokenInput(""); setToken(""); }}>지우기</button>
      </div>
      <h2>일시정지</h2>
      <div className="row">
        <span>현재: {events?.paused ? "일시정지" : "실행 중"}</span>
        <button disabled={!token} onClick={() => call("/api/control/pause", null, "일시정지")}>일시정지</button>
        <button disabled={!token} onClick={() => call("/api/control/resume", null, "재개")}>재개</button>
      </div>
      <h2>계좌 모드</h2>
      <div className="row">
        <select value={acct || accounts[0]?.id || ""} onChange={(e) => setAcct(e.target.value)}>
          {accounts.map((a) => (
            <option key={a.id} value={a.id}>{a.id} ({a.env}) 현재 {a.mode}</option>
          ))}
        </select>
        <select value={mode} onChange={(e) => setMode(e.target.value)}>
          <option value="off">off</option><option value="approve">approve</option><option value="auto">auto</option>
        </select>
        {target?.env === "real" && mode === "auto" && (
          <input placeholder="확인 문자열 LIVE" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
        )}
        <button
          disabled={!token || !target}
          onClick={() =>
            call(
              "/api/control/mode",
              { account_id: target?.id, mode, confirm: confirm || undefined },
              `${target?.id} → ${mode}`,
            )
          }
        >
          변경
        </button>
      </div>
      <p className="muted">실전 계좌의 auto 는 확인 문자열 LIVE 가 있어야 하며, 없으면 서버가 거부한다.</p>
      {msg && <p>{msg}</p>}
      <h2>설정 (settings.yaml 요약)</h2>
      <ErrorBox error={settings.error} />
      {settings.data && <JsonView value={settings.data} />}
      <h2>STATUS.md</h2>
      <pre className="status">{status.data?.markdown || "(없음)"}</pre>
    </section>
  );
}
