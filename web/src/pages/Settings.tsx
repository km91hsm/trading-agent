import { useState } from "react";
import { getJson, postJson } from "../api";
import { Badge } from "../components/Badge";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { Glyph } from "../components/Glyph";
import { StageReport } from "../components/StageReport";
import { settingsBlocks } from "../report";
import { Section } from "../components/Section";
import { ErrorBox } from "../components/Tables";
import { Term } from "../components/Term";
import { useToast } from "../components/Toast";
import { termHelp } from "../glossary";
import { useApp, useLoad } from "../hooks";
import { parseWatch } from "../tape";
import type { Theme } from "../theme";
import { Ticker } from "../components/Ticker";

const THEMES: Array<[Theme, string]> = [
  ["light", "라이트"],
  ["dark", "다크"],
  ["system", "시스템"],
];

export function SettingsPage() {
  const { accounts, token, setToken, events, reloadAccounts, tick, theme, setTheme } = useApp();
  const toast = useToast();
  const [tokenInput, setTokenInput] = useState(token ?? "");
  const [acct, setAcct] = useState("");
  const [mode, setMode] = useState("off");
  const [confirmText, setConfirmText] = useState("");
  const [ask, setAsk] = useState<null | "mode">(null);
  const [watchInput, setWatchInput] = useState("");
  const settings = useLoad(() => getJson<Record<string, unknown>>("/api/system/settings"), [tick]);
  const status = useLoad(() => getJson<{ markdown: string }>("/api/system/status"), [tick]);
  const watch = useLoad(() => getJson<{ tickers: string[] }>("/api/control/watchlist"), [tick]);
  const target = accounts.find((a) => a.id === (acct || accounts[0]?.id));
  const needsLive = target?.env === "real" && mode === "auto";

  const call = async (path: string, body: unknown, ok: string) => {
    try {
      await postJson(path, body, token);
      toast(ok, "ok");
      reloadAccounts();
    } catch (e) {
      toast(`실패: ${(e as Error).message}`, "error");
    }
  };
  const changeMode = () =>
    call("/api/control/mode", { account_id: target?.id, mode, confirm: needsLive ? confirmText : undefined }, `${target?.id} 모드 → ${mode}`);
  const changeWatch = async (action: "add" | "remove", t: string) => {
    try {
      await postJson("/api/control/watchlist", { action, ticker: t }, token);
      toast(`관심종목 ${action === "add" ? "추가" : "제거"}: ${t}`, "ok");
      setWatchInput("");
      watch.reload();
    } catch (e) {
      toast(`실패: ${(e as Error).message}`, "error");
    }
  };

  return (
    <>
      <header className="page-h">
        <h1>설정/제어</h1>
        <p className="sub">제어(승인·일시정지·모드·관심종목)는 이 브라우저에 저장한 토큰으로만 보낸다. 조회는 토큰 없이도 된다.</p>
      </header>
      <div className="cols-2">
        <div>
          <Section title="제어 토큰" aside={token ? <Badge kind="ok" value="저장됨" /> : <Badge kind="neutral" value="없음" />}>
            <p className="note">secrets/.env 의 WEB_TOKEN 값. 이 브라우저(localStorage)에만 저장되고 서버로는 요청 헤더로만 간다.</p>
            <form
              className="row"
              onSubmit={(e) => {
                e.preventDefault();
                setToken(tokenInput.trim());
                toast(tokenInput.trim() ? "토큰을 저장했어요 (이 브라우저에만)" : "토큰을 지웠어요", "ok");
              }}
            >
              <label className="field">
                <span>WEB_TOKEN</span>
                <input type="password" autoComplete="off" value={tokenInput} onChange={(e) => setTokenInput(e.target.value)} size={28} />
              </label>
              <button type="submit" className="btn btn-primary">
                저장
              </button>
              <button
                type="button"
                className="btn"
                onClick={() => {
                  setTokenInput("");
                  setToken("");
                  toast("토큰을 지웠어요", "ok");
                }}
              >
                지우기
              </button>
            </form>
          </Section>
          <Section title="거래" aside={events?.paused ? <Badge kind="pending" value="일시정지" /> : <Badge kind="ok" value="실행 중" />}>
            <p className="note">
              <Term k="paused" /> 상태에서는 판정만 기록하고 주문·승인 요청을 내지 않는다. 데몬·텔레그램도 같은 플래그를 본다.
            </p>
            <div className="row">
              <button type="button" className="btn btn-danger" disabled={!token || !!events?.paused} onClick={() => call("/api/control/pause", null, "일시정지했어요")}>
                일시정지
              </button>
              <button type="button" className="btn btn-primary" disabled={!token || !events?.paused} onClick={() => call("/api/control/resume", null, "재개했어요")}>
                재개
              </button>
            </div>
          </Section>
          <Section title="계좌 모드" aside="실전 auto 는 LIVE 확인 필요">
            <div className="row">
              <label className="field">
                <span>계좌</span>
                <select value={acct || accounts[0]?.id || ""} onChange={(e) => setAcct(e.target.value)}>
                  {accounts.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.id} · {a.env === "real" ? "실전" : "모의"} · 현재 {a.mode}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>바꿀 모드</span>
                <select value={mode} onChange={(e) => setMode(e.target.value)}>
                  <option value="off">off · 기록만</option>
                  <option value="approve">approve · 승인 뒤 주문</option>
                  <option value="auto">auto · 자동 주문</option>
                </select>
              </label>
              {needsLive && (
                <label className="field">
                  <span>확인 문자열</span>
                  <input type="text" placeholder="LIVE" value={confirmText} onChange={(e) => setConfirmText(e.target.value)} size={8} />
                </label>
              )}
              <button
                type="button"
                className={needsLive ? "btn btn-danger" : "btn btn-primary"}
                disabled={!token || !target || (needsLive && confirmText !== "LIVE")}
                onClick={() => (target?.env === "real" ? setAsk("mode") : void changeMode())}
              >
                변경
              </button>
            </div>
            {target && (
              <p className="note">
                {target.id}: 현재 <Badge kind="mode" value={target.mode} /> · yaml {target.yaml_mode} · 바꾸면{" "}
                <Term k={`mode.${mode}`} /> — {termHelp(`mode.${mode}`)}
              </p>
            )}
          </Section>
        </div>
        <div>
          <Section title="관심종목" aside="런타임 · universe.yaml 의 watch 와 합쳐 분석 대상">
            <ErrorBox error={watch.error} />
            <form
              className="row"
              onSubmit={(e) => {
                e.preventDefault();
                if (watchInput.trim()) void changeWatch("add", watchInput.trim());
              }}
            >
              <label className="field">
                <span>종목 (005930 · KR:005930 · US:AAPL)</span>
                <input type="text" value={watchInput} onChange={(e) => setWatchInput(e.target.value)} size={16} />
              </label>
              <button type="submit" className="btn btn-primary" disabled={!token || !watchInput.trim()}>
                추가
              </button>
            </form>
            <div className="row tight chips">
              {(watch.data?.tickers ?? []).map((t) => (
                <span className="chip" key={t}>
                  <Ticker market={parseWatch(t)[0]} ticker={parseWatch(t)[1]} />
                  <button type="button" aria-label={`${t} 제거`} disabled={!token} onClick={() => changeWatch("remove", t)}>
                    <Glyph name="x" size={12} />
                  </button>
                </span>
              ))}
              {watch.data && !watch.data.tickers.length && <span className="note">없음</span>}
            </div>
          </Section>
          <Section title="테마">
            <div className="row tight">
              {THEMES.map(([t, label]) => (
                <button key={t} type="button" className={theme === t ? "btn btn-sm sel" : "btn btn-sm btn-ghost"} onClick={() => setTheme(t)}>
                  {label}
                </button>
              ))}
            </div>
          </Section>
          <Section title="설정 요약" aside="settings.yaml · 시크릿 없음">
            <ErrorBox error={settings.error} />
            <div className="viewer">{settings.data && <StageReport data={settings.data} blocks={settingsBlocks(settings.data)} />}</div>
          </Section>
        </div>
      </div>
      <Section title="STATUS.md" aside="`trader status` 가 만든 요약">
        <pre className="status">{status.data?.markdown || "(없음)"}</pre>
      </Section>
      <ConfirmDialog
        open={ask === "mode"}
        title={`실전 계좌 ${target?.id} 모드를 ${mode} 로 바꿉니다`}
        body={
          mode === "auto"
            ? "auto 는 리스크 게이트를 통과한 주문을 사람 확인 없이 실제 계좌로 냅니다. 확인 기록(live_confirmed)이 남습니다."
            : mode === "approve"
              ? "approve 는 주문 전에 승인을 기다립니다(30분 만료)."
              : "off 는 판정만 기록하고 주문을 내지 않습니다."
        }
        confirmLabel="변경"
        danger={mode === "auto"}
        onConfirm={() => {
          setAsk(null);
          void changeMode();
        }}
        onClose={() => setAsk(null)}
      />
    </>
  );
}
