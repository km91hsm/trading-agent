// 앱 셸: 상단 바(브랜드·계좌·장 상태·상태 배지·테마) + 좌측 내비(좁으면 서랍) + 본문.
import { useEffect, useState, type ReactNode } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { useApp } from "../hooks";
import { marketPhase } from "../tape";
import type { Theme } from "../theme";
import { Badge } from "./Badge";
import { Glyph, type GlyphName } from "./Glyph";

export const LINKS: Array<[string, string]> = [
  ["/", "대시보드"],
  ["/today", "오늘의 실행"],
  ["/orders", "주문/승인"],
  ["/account", "계좌"],
  ["/compare", "계좌 비교"],
  ["/chart", "차트"],
  ["/performance", "성과"],
  ["/data", "데이터"],
  ["/ml", "ML"],
  ["/settings", "설정/제어"],
];

const THEME_NEXT: Record<Theme, Theme> = { light: "dark", dark: "system", system: "light" };
const THEME_GLYPH: Record<Theme, GlyphName> = { light: "sun", dark: "moon", system: "system" };
const THEME_LABEL: Record<Theme, string> = { light: "라이트", dark: "다크", system: "시스템" };

export function Shell({ children, now = new Date() }: { children: ReactNode; now?: Date }) {
  const { accounts, accountId, setAccountId, token, events, theme, setTheme } = useApp();
  const [open, setOpen] = useState(false);
  const loc = useLocation();
  useEffect(() => setOpen(false), [loc.pathname]);
  const acct = accounts.find((a) => a.id === accountId);
  const env = acct?.env ?? "paper";
  const phase = marketPhase(now);
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
          <select value={accountId} onChange={(e) => setAccountId(e.target.value)} aria-label="계좌 선택">
            {accounts.map((a) => (
              <option key={a.id} value={a.id}>
                {a.id} · {a.env === "real" ? "실전" : "모의"} · {a.mode}
              </option>
            ))}
            {!accounts.length && <option value="">계좌 없음</option>}
          </select>
          {acct && <Badge kind="env" value={acct.env} />}
        </label>
        <span className="phase muted">{phase.label}</span>
        <span className="spacer" />
        <div className="status-badges">
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
        {LINKS.map(([to, label]) => (
          <NavLink key={to} to={to} end={to === "/"}>
            {label}
          </NavLink>
        ))}
      </nav>
      <main>{children}</main>
    </div>
  );
}
