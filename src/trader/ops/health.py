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
from trader.notify import messages
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
    items += _reconcile(conn, settings, accounts, now, schedule_jobs or [], state)
    items += _jobs(conn, now, state, schedule_jobs or [])
    items += _llm(conn, settings, now)
    items += _disclosure(conn, settings, accounts, now)
    areas: dict[str, str] = {}
    for it in items:
        area = it.key.split(".")[0]
        areas[area] = worst([areas.get(area, "ok"), it.level])
    for area in ("loss", "orders", "reconcile", "jobs", "llm", "disclosure"):
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


DISCLOSURE_DAYS = 3


def _disclosure(conn, settings, accounts, now) -> list[Item]:
    """보유 종목(최신 스냅샷)의 최근 3일 critical 공시(Phase 25) → critical 단계."""
    from trader.ledger import views
    from trader.sources import disclosures

    out: list[Item] = []
    for market in settings.markets:
        held: set[str] = set()
        for a in accounts:
            if market not in a.markets:
                continue
            snap = views.latest_positions(conn, a.id, market)
            held |= {p["ticker"] for p in (snap or {}).get("positions") or [] if p["qty"]}
        if not held:
            continue
        rows = disclosures.critical_recent(
            conn, market, since=now - timedelta(days=DISCLOSURE_DAYS), tickers=held
        )
        if rows:
            out.append(
                Item(
                    f"disclosure.{market}",
                    "critical",
                    f"{market} 보유 종목 중요 공시 {len(rows)}건 — {rows[-1]['title']}",
                    link="/data",
                    count=len(rows),
                )
            )
    return out


def _loss(conn, settings, accounts, now) -> list[Item]:
    from trader.risk import killswitch

    out: list[Item] = []
    ks = killswitch.reason(conn)
    if ks:
        out.append(
            Item(
                "loss.killswitch",
                "critical",
                f"킬스위치 켜짐 — 신규 매수 중단: {ks}. 해제는 "
                "`trader control killswitch off --note` (원인 메모)",
                link="/settings",
            )
        )
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


def _reconcile_due(m: str, local_day, schedule_jobs, now) -> datetime:
    """그 시장 운영일(local_day)의 대조 예정 시각(거래소 시간대). 설정 cron(`<시장>_reconcile`,
    cron 시간대 그대로 — KST 06:40 이 전날 ET 17:40 인 것처럼 날짜가 넘어가는 경우 포함)의 최근 실행
    시각을 거래소 시각으로 바꿔 쓰고, 잡이 없으면 현지 16:40(Codex 리뷰)."""
    for j in schedule_jobs:
        if getattr(j, "id", None) == f"{m.lower()}_reconcile" and getattr(j, "cron", None):
            from trader.daemon import _trigger

            trig = _trigger(j)
            if hasattr(trig, "get_next_fire_time"):
                start = datetime.combine(local_day, time(0, 0), tzinfo=MARKET_TZ[m])
                fire = trig.get_next_fire_time(None, start)
                if fire is not None:
                    return fire.astimezone(MARKET_TZ[m])
    return datetime.combine(local_day, time(16, 40), tzinfo=MARKET_TZ[m])


def _reconcile_skipped_today(m: str, local_day, state, schedule_jobs, now) -> bool:
    """오늘 대조 잡이 휴장·주말·일시정지로 건너뛰었는가(하트비트) — KR 휴장일은 KIS 조회 없이는
    몰라 잡의 판단을 따른다. US 는 NYSE 규칙으로 바로 안다."""
    if m == "US":
        from trader.daemon import us_holidays

        if local_day in us_holidays(local_day.year):
            return True
    try:
        base = state if state is not None else paths.state_dir()
        st = heartbeat.read_status(base, now, schedule_jobs)
    except Exception:  # noqa: BLE001 - 점검 보조 정보
        return False
    for j in st["jobs"]:
        if j.get("id") == f"{m.lower()}_reconcile" and j.get("last_status") == "skipped":
            end = j.get("last_end")
            if end and datetime.fromisoformat(end).astimezone(MARKET_TZ[m]).date() == local_day:
                return True
    return False


def _reconcile(conn, settings, accounts, now, schedule_jobs=(), state=None) -> list[Item]:
    out: list[Item] = []
    for a in accounts:
        for m in a.markets:
            d = run_date_for(m, now)
            flag = control.get_flag(conn, f"reconcile:{a.id}:{m}:{d.isoformat()}")
            local = now.astimezone(MARKET_TZ[m])
            due = _reconcile_due(m, local.date(), schedule_jobs, now) + RECONCILE_GRACE
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
            elif (
                local.weekday() < 5
                and local >= due
                and not _reconcile_skipped_today(m, local.date(), state, schedule_jobs, now)
            ):
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
    since = (now - timedelta(hours=24)).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) AS n, (SELECT kind FROM runs WHERE status='error' AND started_at >= ? "
        "ORDER BY started_at DESC, id DESC LIMIT 1) AS k "
        "FROM runs WHERE status='error' AND started_at >= ?",
        (since, since),
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


ALERT_FROM = "warning"  # 이 단계 이상 항목은 새로 생기면 단계가 같아도 알린다


def _alert_keys(result: dict) -> set[str]:
    return {
        str(i["key"])
        for i in result["items"]
        if i.get("key") and LEVELS.index(i.get("level", "ok")) >= LEVELS.index(ALERT_FROM)
    }


def escalate(conn: sqlite3.Connection, result: dict) -> str | None:
    """단계가 나빠졌거나, 같은 단계라도 경고 이상 **새 항목**이 생겼으면 알림 문구를 돌려준다.
    현재 단계와 경고 항목 키를 `control.ops_level` 에 적는다(좋아져도 적되 알림은 없다).
    오래 가는 경고(잡 오류 등) 하나에 새 상한 도달·손실 경고가 묻히던 문제(전체 검토
    2026-10-27). 전송 실패는 알림 보관함이 다시 보낸다."""
    prev = control.get_flag(conn, LEVEL_FLAG) or {}
    prev_level = str(prev.get("level") or "ok")
    prev_keys = set(prev.get("keys") or [])
    keys = _alert_keys(result)
    if (
        result["level"] != prev.get("level")
        or not prev.get("at")
        or sorted(keys) != sorted(prev_keys)
    ):
        # at = 이 단계가 된 시각. 단계·경고 키가 같으면 다시 쓰지 않는다(control 지문, 검토 3차)
        control.set_flag(
            conn, LEVEL_FLAG, {"level": result["level"], "at": result["at"], "keys": sorted(keys)}
        )
    worse = LEVELS.index(result["level"]) > LEVELS.index(prev_level)
    new = keys - prev_keys
    if not worse and not new:
        return None
    items = result["items"]
    if not worse:
        items = [i for i in items if i["key"] in new]
    heads = "; ".join(i["text"] for i in items[:4])
    title = f"{LABEL[prev_level]} → {result['label']}" if worse else f"새 경고 {len(new)}건"
    return messages.event("점검 격상", title, heads)


def headline(result: dict | None) -> str:
    if not result:
        return "점검 기록 없음"
    at = datetime.fromisoformat(result["at"]).astimezone(MARKET_TZ["KR"]).strftime("%H:%M")
    if result["level"] == "ok":
        return f"점검 정상 · {at}"
    return f"점검 {result['label']} · {len(result['items'])}건 · {at}"


def today_key(now: datetime) -> date:
    return run_date_for("KR", now)
