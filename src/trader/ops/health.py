"""운영 점검 (Phase 23 A) — 원장·하트비트·플래그를 읽어 다섯 영역의 단계를 판정한다.

영역: `loss`(실현손익) · `orders`(unknown·마감 뒤 미정리·승인 만료·체결 매칭 모호) ·
`reconcile`(야간 대조 불일치·미실행) · `jobs`(데몬 생존·stale·24h 오류·놓친 cron) ·
`llm`(세션 실패·상한 근접).
단계 `ok < watch < warning < critical`, 머리 단계는 항목 중 최악. 판정은 읽기 전용이다 — 어떤 상태도
바꾸지 않는다(알림·격상 기록은 데몬 `ops_check` 잡)."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from trader import heartbeat
from trader.config import MARKET_TZ, AccountConfig, Settings
from trader.control import control
from trader.ledger import repo
from trader.ledger.repo import dead_status_sql
from trader.orchestrator import tools
from trader.orchestrator.run_store import run_date_for
from trader.platform import paths

LEVELS = ("ok", "watch", "warning", "critical")
LABEL = {"ok": "정상", "watch": "관심", "warning": "주의", "critical": "경보"}
LOSS_WATCH = Decimal("-0.02")
LOSS_WARNING = Decimal("-0.05")
LOSS_CRITICAL_5D = Decimal("-0.10")
LLM_CAP_WATCH = 0.8
RECONCILE_GRACE = timedelta(minutes=30)
MARKET_CLOSE: dict[str, time] = {"KR": time(15, 30), "US": time(16, 0)}


@dataclass
class Item:
    key: str  # 영역.세부 (예: orders.unknown)
    level: str
    text: str
    link: str | None = None  # 웹 경로 (조치 필요 행이 가리킨다)
    count: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def worst(levels: list[str]) -> str:
    return max(levels, key=LEVELS.index) if levels else "ok"


def check(
    conn: sqlite3.Connection,
    settings: Settings,
    accounts: list[AccountConfig],
    *,
    now: datetime | None = None,
    state: Path | None = None,
    schedule_jobs: list[Any] | None = None,
) -> dict:
    """{level, label, at, items:[Item], areas:{영역: level}, actions:[...]}."""
    now = now or datetime.now(UTC)
    items: list[Item] = []
    items += _loss(conn, settings, accounts, now)
    items += _orders(conn, settings, accounts, now)
    items += _reconcile(conn, settings, accounts, now)
    items += _jobs(conn, now, state, schedule_jobs or [])
    items += _llm(conn, settings, now)
    areas: dict[str, str] = {}
    for it in items:
        area = it.key.split(".")[0]
        areas[area] = worst([areas.get(area, "ok"), it.level])
    for area in ("loss", "orders", "reconcile", "jobs", "llm"):
        areas.setdefault(area, "ok")
    level = worst([it.level for it in items])
    return {
        "level": level,
        "label": LABEL[level],
        "at": now.isoformat(),
        "areas": areas,
        "items": [asdict(it) for it in items if it.level != "ok"],
        "actions": [asdict(it) for it in items if it.link and it.level != "ok"],
    }


# --- 영역 -------------------------------------------------------------------------------


def _loss(conn, settings, accounts, now) -> list[Item]:
    out: list[Item] = []
    for a in accounts:
        for m in a.markets:
            d = run_date_for(m, now)
            nav = repo.latest_nav_before(conn, a.id, d + timedelta(days=1), market=m)
            if not nav or nav <= 0:
                continue
            pnl5 = repo.realized_pnl_5d(conn, a.id, d, market=m)
            pct5 = pnl5 / nav
            if pct5 <= LOSS_CRITICAL_5D:
                lvl = "critical"
            elif pct5 <= LOSS_WARNING:
                lvl = "warning"
            elif pct5 <= LOSS_WATCH:
                lvl = "watch"
            else:
                continue
            out.append(
                Item(
                    f"loss.{a.id}.{m}",
                    lvl,
                    f"{a.id} {m} 최근 5거래일 실현손익 {float(pct5):+.2%} (NAV 대비)",
                    link=f"/account?account_id={a.id}&market={m}",
                    extra={"pct": float(pct5)},
                )
            )
    return out


def _orders(conn, settings, accounts, now) -> list[Item]:
    out: list[Item] = []
    n_unknown = int(
        conn.execute("SELECT COUNT(*) FROM orders WHERE status='unknown'").fetchone()[0]
    )
    if n_unknown:
        out.append(
            Item(
                "orders.unknown",
                "warning",
                f"응답 없는 주문(unknown) {n_unknown}건 — 체결 동기화가 확정하지 못함, KIS 앱 확인",
                link="/orders?status=unknown",
                count=n_unknown,
            )
        )
    # 마감 뒤에도 살아 있는 주문(현지 날짜가 오늘보다 앞선 것)
    stale = 0
    for m in set(mm for a in accounts for mm in a.markets):
        d = run_date_for(m, now)
        lo, _ = repo.day_bounds(d, m)
        local_now = now.astimezone(MARKET_TZ[m])
        closed_today = local_now.time() >= MARKET_CLOSE[m]
        bound = lo if not closed_today else repo.day_bounds(d + timedelta(days=1), m)[0]
        row = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE market=? AND created_at < ? "
            f"AND status NOT IN ({dead_status_sql()},'filled','unknown')",
            (m, bound),
        ).fetchone()
        stale += int(row[0])
    if stale:
        out.append(
            Item(
                "orders.stale",
                "watch",
                f"마감 뒤에도 정리되지 않은 주문 {stale}건 (체결 동기화·대조 확인)",
                link="/orders",
                count=stale,
            )
        )
    n_exp = int(
        conn.execute(
            "SELECT COUNT(*) FROM approvals WHERE status='expired' AND requested_at >= ?",
            ((now - timedelta(hours=24)).isoformat(),),
        ).fetchone()[0]
    )
    if n_exp:
        out.append(
            Item(
                "orders.approvals_expired",
                "watch",
                f"24시간 안에 만료된 승인 요청 {n_exp}건 (응답 없이 지나감)",
                link="/account",
                count=n_exp,
            )
        )
    pending = repo.pending_approvals(conn)
    if pending:
        out.append(
            Item(
                "orders.approvals_pending",
                "watch",
                f"승인 대기 {len(pending)}건 (30분 안에 승인·거부)",
                link="/account",
                count=len(pending),
            )
        )
    for m in set(mm for a in accounts for mm in a.markets):
        d = run_date_for(m, now)
        amb = control.get_flag(conn, f"fill_ambiguous:{m}:{d.isoformat()}")
        if amb:
            out.append(
                Item(
                    f"orders.fill_ambiguous.{m}",
                    "warning",
                    f"{m} 체결 매칭 모호 — unknown 주문 후보가 둘 이상, 사용자 확인 필요",
                    link="/orders?status=unknown",
                    extra=amb if isinstance(amb, dict) else {},
                )
            )
    return out


def _reconcile(conn, settings, accounts, now) -> list[Item]:
    out: list[Item] = []
    for a in accounts:
        for m in a.markets:
            d = run_date_for(m, now)
            flag = control.get_flag(conn, f"reconcile:{a.id}:{m}:{d.isoformat()}")
            local = now.astimezone(MARKET_TZ[m])
            due = (
                datetime.combine(local.date(), time(16, 40), tzinfo=MARKET_TZ[m]) + RECONCILE_GRACE
            )
            if flag:
                if not flag.get("ok", True):
                    n = len(flag.get("mismatches") or []) + len(flag.get("open_orders") or [])
                    out.append(
                        Item(
                            f"reconcile.{a.id}.{m}",
                            "warning",
                            f"{a.id} {m} 야간 대조 불일치 {n}건 (스냅샷은 브로커 값으로 맞춤)",
                            link=f"/account?account_id={a.id}&market={m}",
                            count=n,
                        )
                    )
            elif local.weekday() < 5 and local >= due:
                out.append(
                    Item(
                        f"reconcile.{a.id}.{m}",
                        "watch",
                        f"{a.id} {m} 오늘 야간 대조가 아직 돌지 않음",
                        link="/system",
                    )
                )
    return out


def _jobs(conn, now, state, schedule_jobs) -> list[Item]:
    out: list[Item] = []
    base = state if state is not None else paths.state_dir()
    st = heartbeat.read_status(base, now, schedule_jobs)
    if not st["alive"]:
        out.append(
            Item(
                "jobs.daemon",
                "critical",
                "데몬이 꺼져 있음 (daemon.lock 없음·pid 사망)",
                link="/system",
            )
        )
    elif st["stale"]:
        out.append(
            Item(
                "jobs.stale",
                "critical",
                f"데몬이 살아 있지만 하트비트가 {int(st['age_s'] or 0) // 60}분 멈춤",
                link="/system",
            )
        )
    missed = [j["id"] for j in st["jobs"] if j.get("last_status") == "missed"]
    if missed:
        out.append(
            Item(
                "jobs.missed",
                "warning",
                f"놓친 잡 {len(missed)}건: {', '.join(missed[:4])}",
                link="/system",
                count=len(missed),
            )
        )
    row = conn.execute(
        "SELECT COUNT(*) AS n, MAX(kind) AS k FROM runs WHERE status='error' AND started_at >= ?",
        ((now - timedelta(hours=24)).isoformat(),),
    ).fetchone()
    if row and int(row["n"]):
        out.append(
            Item(
                "jobs.errors",
                "warning" if int(row["n"]) >= 3 else "watch",
                f"24시간 안에 오류로 끝난 잡 {int(row['n'])}건 (최근 {row['k']})",
                link="/system",
                count=int(row["n"]),
            )
        )
    return out


def _llm(conn, settings, now) -> list[Item]:
    out: list[Item] = []
    cap = settings.llm.daily_session_cap
    n = tools.sessions_today(conn, now)
    if cap and n >= cap:
        out.append(
            Item(
                "llm.cap",
                "warning",
                f"오늘 세션 {n}/{cap} — 상한 도달, 남은 분석·재평가는 거부됨",
                link="/system",
            )
        )
    elif cap and n >= cap * LLM_CAP_WATCH:
        out.append(Item("llm.cap", "watch", f"오늘 세션 {n}/{cap} — 상한 근접", link="/system"))
    # 구독 사용률 감속(Phase 24): soft 면 watch(새 분석 생략), hard 면 warning(세션 전부 중지)
    from trader import usage

    ok_new, why = usage.session_allowance(conn, settings, kind="analyze", now=now)
    if not ok_new:
        ok_re, why_re = usage.session_allowance(conn, settings, kind="resume", now=now)
        out.append(
            Item(
                "llm.usage",
                "watch" if ok_re else "warning",
                why if ok_re else (why_re or why or "구독 사용률 감속"),
                link="/system",
            )
        )
    row = conn.execute(
        "SELECT COUNT(*) AS n, SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS e "
        "FROM agent_calls WHERE agent='orchestrator' AND COALESCE(model,'') != 'fake' "
        "AND created_at >= ?",
        ((now - timedelta(hours=24)).isoformat(),),
    ).fetchone()
    total, err = int(row["n"] or 0), int(row["e"] or 0)
    if total >= 3 and err / total >= 0.5:
        out.append(
            Item(
                "llm.errors",
                "critical",
                f"24시간 세션 {total}건 중 {err}건 실패 — claude CLI·인증·네트워크 확인",
                link="/system",
                count=err,
            )
        )
    elif err:
        out.append(
            Item(
                "llm.errors",
                "watch",
                f"24시간 세션 {total}건 중 {err}건 실패",
                link="/system",
                count=err,
            )
        )
    return out


# --- 격상 기록 (데몬) --------------------------------------------------------------------

LEVEL_FLAG = "ops_level"
RESULT_FLAG = "ops_result"  # 마지막 점검 결과 전체(웹·텔레그램·STATUS 공용)


def escalate(conn: sqlite3.Connection, result: dict) -> str | None:
    """직전 단계보다 나빠졌으면 알림 문구를 돌려주고 현재 단계를 `control.ops_level` 에 적는다
    (좋아진 것도 적되 알림은 없다)."""
    prev = control.get_flag(conn, LEVEL_FLAG) or {}
    prev_level = str(prev.get("level") or "ok")
    control.set_flag(conn, LEVEL_FLAG, {"level": result["level"], "at": result["at"]})
    if LEVELS.index(result["level"]) <= LEVELS.index(prev_level):
        return None
    heads = "; ".join(i["text"] for i in result["items"][:4])
    return f"[점검 격상] {LABEL[prev_level]} → {result['label']}: {heads}"


def headline(result: dict | None) -> str:
    if not result:
        return "점검 기록 없음"
    at = datetime.fromisoformat(result["at"]).astimezone(MARKET_TZ["KR"]).strftime("%H:%M")
    if result["level"] == "ok":
        return f"점검 정상 · {at}"
    return f"점검 {result['label']} · {len(result['items'])}건 · {at}"


def today_key(now: datetime) -> date:
    return run_date_for("KR", now)
