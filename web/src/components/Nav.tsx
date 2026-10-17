import { NavLink } from "react-router-dom";
import { useApp } from "../hooks";

const LINKS: Array<[string, string]> = [
  ["/", "대시보드"],
  ["/account", "계좌 상세"],
  ["/compare", "계좌 비교"],
  ["/today", "오늘의 실행"],
  ["/orders", "주문/승인"],
  ["/chart", "차트"],
  ["/data", "데이터"],
  ["/ml", "ML"],
  ["/performance", "성과"],
  ["/settings", "설정/제어"],
];

export function Nav() {
  const { events, token } = useApp();
  return (
    <nav className="nav">
      <span className="brand">trader</span>
      {LINKS.map(([to, label]) => (
        <NavLink key={to} to={to} end={to === "/"}>
          {label}
        </NavLink>
      ))}
      <span className="spacer" />
      {events?.paused && <span className="badge warn">일시정지</span>}
      {events && events.approvals_pending > 0 && (
        <span className="badge">미승인 {events.approvals_pending}</span>
      )}
      <span className={`badge ${token ? "ok" : "muted"}`}>{token ? "토큰 있음" : "조회 전용"}</span>
    </nav>
  );
}
