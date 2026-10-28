// 앱 셸: 상단 바(브랜드·계좌·장 상태·상태 배지·테마) + 좌측 내비(좁으면 서랍) + 본문.
import { useState, type ReactNode } from "react";
import { NavLink } from "react-router-dom";
import { useApp } from "../hooks";
import type { Theme } from "../theme";
import { MOBILE_TABS, NAV } from "../nav";
import { ALL_ACCOUNTS } from "../overview";
import { Badge } from "./Badge";
import { Clock } from "./Clock";
import { IndexChips } from "./IndexChips";
import { CommandPalette } from "./CommandPalette";
import { Glyph, type GlyphName } from "./Glyph";
import { modeAccountLabel } from "../fo";

const THEME_NEXT: Record<Theme, Theme> = { light: "dark", dark: "system", system: "light" };
const THEME_GLYPH: Record<Theme, GlyphName> = { light: "sun", dark: "moon", system: "system" };
const THEME_LABEL: Record<Theme, string> = { light: "라이트", dark: "다크", system: "시스템" };

export function Shell({ children }: { children: ReactNode }) {
  const { accounts, accountId, selection, setAccountId, token, events, connected, theme, setTheme } = useApp();
  const [open, setOpen] = useState(false);
  const acct = accounts.find((a) => a.id === accountId);
  // '전체'(기본)에서 실전 계좌가 섞여 있으면 실전 표식을 유지한다 — 모의처럼 보이지 않게(전체 검토 W6)
  const hasReal = accounts.some((a) => a.env === "real");
  const env = acct?.env ?? (hasReal ? "real" : "paper");
  return (
    <div className="shell" data-env={env}>
      <header className="topbar">
        <button
          type="button"
          className="nav-toggle"
          aria-label={open ? "메뉴 닫기" : "메뉴 열기"}
          aria-expanded={open}
          aria-controls="sidenav"
          onClick={() => setOpen((o) => !o)}
        >
          <Glyph name={open ? "close" : "menu"} />
        </button>
        <NavLink to="/" className="brand">
          <Glyph name="mark" size={18} />
          <span>trader</span>
        </NavLink>
        <label className="acct" data-env={env}>
          <span className="sr-only">계좌</span>
          <select value={selection} onChange={(e) => setAccountId(e.target.value)} aria-label="계좌 선택">
            {accounts.length > 0 && <option value={ALL_ACCOUNTS}>전체 ({accounts.length}개 합산)</option>}
            {accounts.map((a) => (
              <option key={a.id} value={a.id}>
                {modeAccountLabel(a)}
              </option>
            ))}
            {!accounts.length && <option value="">계좌 없음</option>}
          </select>
          {acct ? <Badge kind="env" value={acct.env} /> : hasReal ? <Badge kind="env" value="real" title="전체 합산에 실전 계좌가 들어 있어요" /> : null}
        </label>
        <Clock />
        <IndexChips />
        <span className="spacer" />
        <div className="status-badges">
          {!connected && <Badge kind="pending" value="실시간 끊김" title="서버와의 실시간 연결(SSE)이 끊겨 재연결 중 — 1분마다 서버 생존을 확인해 표를 다시 읽어요" />}
          {events?.paused && <Badge kind="pending" value="일시정지" title="거래 중단 중 — 설정/제어에서 재개" />}
          {events && events.approvals_pending > 0 && (
            <NavLink to="/orders" className="badge-link">
              <Badge kind="pending" value={`미승인 ${events.approvals_pending}`} title="승인을 기다리는 주문" />
            </NavLink>
          )}
          {token ? (
            <Badge kind="ok" value="제어 가능" title="토큰이 있어 승인·일시정지·모드를 바꿀 수 있다" />
          ) : (
            <Badge kind="neutral" value="조회 전용" title="설정/제어에서 토큰을 넣으면 제어할 수 있다" />
          )}
        </div>
        <button
          type="button"
          className="theme-btn"
          onClick={() => setTheme(THEME_NEXT[theme])}
          aria-label={`테마: ${THEME_LABEL[theme]} (누르면 ${THEME_LABEL[THEME_NEXT[theme]]})`}
          title={`테마: ${THEME_LABEL[theme]}`}
        >
          <Glyph name={THEME_GLYPH[theme]} />
        </button>
      </header>
      <nav id="sidenav" className={open ? "sidenav open" : "sidenav"} aria-label="주 메뉴">
        {NAV.map((g) => (
          <div key={g.id} className="nav-group">
            <div className="nav-group-title muted">{g.title}</div>
            {g.items.map((it) => (
              <NavLink key={it.to} to={it.to} end={it.to === "/"} onClick={() => setOpen(false)} title={it.purpose}>
                {it.label}
              </NavLink>
            ))}
          </div>
        ))}
        <p className="muted nav-hint">⌘K / Ctrl+K 이동</p>
      </nav>
      <main>{children}</main>
      <nav className="tabbar" aria-label="하단 탭">
        {MOBILE_TABS.map((it) => (
          <NavLink key={it.to} to={it.to} end={it.to === "/"}>
            {it.label}
          </NavLink>
        ))}
        <button type="button" onClick={() => setOpen(true)} aria-label="전체 메뉴">
          전체
        </button>
      </nav>
      <CommandPalette />
    </div>
  );
}
