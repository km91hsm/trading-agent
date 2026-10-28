"""운영 점검 (Phase 23 A) — 원장·하트비트·플래그를 읽어 다섯 영역의 단계를 판정한다.

영역: `loss`(실현손익) · `orders`(unknown·마감 뒤 미정리·승인 만료·체결 매칭 모호) ·
`reconcile`(야간 대조 불일치·미실행) · `jobs`(데몬 생존·stale·24h 오류·놓친 cron) ·
`llm`(세션 실패·상한 근접).
단계 `ok < watch < warning < critical`, 머리 단계는 항목 중 최악. 판정은 읽기 전용이다 — 어떤 상태도
바꾸지 않는다(알림·격상 기록은 데몬 `ops_check` 잡)."""

from __future__ import annotations

import logging
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

logger = logging.getLogger("trader.ops")

LEVELS = ("ok", "watch", "warning", "critical")
LABEL = {"ok": "정상", "watch": "관심", "warning": "주의", "critical": "경보"}
LOSS_WATCH = Decimal("-0.02")
LOSS_WARNING = Decimal("-0.05")
LOSS_CRITICAL_5D = Decimal("-0.10")
FLOW_SHOW_DAYS = 2  # 입출금 의심(2-O16)을 기록 뒤 며칠(운영일) 보일지
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
    items += _fo(conn, settings, accounts, now)
    items += _saveticker(now)
    areas: dict[str, str] = {}
    for it in items:
        area = it.key.split(".")[0]
        areas[area] = worst([areas.get(area, "ok"), it.level])
    for area in ("loss", "orders", "reconcile", "jobs", "llm", "disclosure", "fo", "saveticker"):
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
            flow = killswitch.flow_suspicion(conn, a.id, m)
            if flow and (d - date.fromisoformat(str(flow.get("date")))).days <= FLOW_SHOW_DAYS:
                out.append(
                    Item(
                        f"loss.flow.{a.id}.{m}",
                        "warning",
                        f"{a.id} {m} 입출금 의심 — 가격으로 설명되지 않는 NAV 변화 "
                        f"{flow.get('residual')} ({flow.get('date')}). 킬스위치 기준이 그대로라 "
                        "출금은 손실로, 입금은 손실 은폐로 보입니다. 확인 뒤 "
                        "`trader control killswitch off --note` 로 기준을 다시 잡으세요"
                        "(모든 계좌·시장의 기준을 최근 NAV 로 다시 잡습니다)",
                        link="/settings",
                    )
                )
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
    from trader.broker import rate_store

    out: list[Item] = []
    for d in rate_store.disabled_status():  # 같은 프로세스(데몬)의 리미터만 보인다(2-P8)
        out.append(
            Item(
                "orders.rate_limiter",
                "warning",
                f"KIS 교차 프로세스 레이트리미터 꺼짐({d['env']}, {d['error']}) — 데몬과 "
                "분석 도구가 초당 한도를 나누지 못해 EGW00201 이 날 수 있음. "
                "state/kis_rate.db 권한·디스크 확인",
            )
        )
    n_unknown = int(
        conn.execute(
            "SELECT COUNT(*) FROM orders WHERE status='unknown' AND asset='stock'"
        ).fetchone()[0]
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
    stock_accounts = [a for a in accounts if getattr(a, "asset", "stock") == "stock"]
    for m in set(mm for a in stock_accounts for mm in a.markets):
        d = run_date_for(m, now)
        lo, _ = repo.day_bounds(d, m)
        local_now = now.astimezone(MARKET_TZ[m])
        closed_today = local_now.time() >= MARKET_CLOSE[m]
        bound = lo if not closed_today else repo.day_bounds(d + timedelta(days=1), m)[0]
        row = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE market=? AND created_at < ? AND asset='stock' "
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
    try:  # 보정 재검토 시점(결정 14번): 만기 결정이 기준에 닿았는데 주입이 꺼져 있다
        from trader.config import load_ml
        from trader.memory import store

        cal = load_ml().models.calibration
        n = store.matured_count(conn)
        if cal.enabled and not cal.inject and n >= cal.min_decisions:
            out.append(
                Item(
                    "llm.calibration_review",
                    "watch",
                    f"만기 결정 {n}건 — 보정 재검토 시점. L12 개선(벤치마크 대비 부호·Wilson 하한·"
                    "수축, 역할·이웃 표본 수 조건) 뒤 `ml.yaml calibration.inject` 를 켠다 "
                    "(`trader calibration build` 로 리포트 확인)",
                    link="/ml",
                    count=n,
                )
            )
    except Exception:  # noqa: BLE001 - 점검 보조
        pass
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


# --- 파생(Phase 26) ----------------------------------------------------------------------

FO_SETTLE_GRACE_DAYS = 1


def _saveticker(now) -> list[Item]:
    """세이브티커 수집기(계정 파일이 있을 때만): 서비스 정지 warning, 접근이 막혀 쉬는 작업 watch.
    데몬 감시 잡은 정지 상태여도 'ok' 로 끝나 보이지 않던 문제(2026-10-29)."""
    from trader import saveticker as st
    from trader.saveticker import automation

    try:
        if not st.credentials_path().is_file():
            return []
        home = st.home()
        s = automation.status(home)
        paused = automation.active_skip_lanes(home, now=now)
    except Exception as e:  # noqa: BLE001 — 점검은 읽기 전용, 실패가 다른 영역을 막지 않는다
        logger.debug("세이브티커 점검 실패: %s", e)
        return []
    out: list[Item] = []
    if s["blocked"]:
        text = f"세이브티커 수집 멈춤 — {s['reason_text']}"
        out.append(Item("saveticker.stopped", "warning", text, link="/settings"))
    if paused:
        text = f"세이브티커 {', '.join(paused)} 작업 쉬는 중(접근 거부, 6시간 뒤 다시 시도)"
        out.append(Item("saveticker.lane_paused", "watch", text, link="/settings"))
    return out


def _fo(conn, settings, accounts, now) -> list[Item]:
    """파생 레인 점검: unknown·exit_failed 포지션(critical), 정산 대기 1운영일 초과(warning),
    옵션 매도 최종거래일 D-1 미청산(critical), 파생 마감 뒤 살아 있는 주문(watch)."""
    from trader.market_hours import fo_close_hm

    out: list[Item] = []
    fo_accounts = [a for a in accounts if getattr(a, "asset", "stock") == "fo"]
    if not fo_accounts:
        return out
    ids = [a.id for a in fo_accounts]
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, account_id, code, status, vehicle, last_trade_date, closed_at, created_at "
        f"FROM fo_positions WHERE account_id IN ({marks}) AND status NOT IN ('closed','settled')",
        ids,
    ).fetchall()
    today = run_date_for("KR", now)
    bad = [r for r in rows if r["status"] in ("unknown", "exit_failed")]
    if bad:
        out.append(
            Item(
                "fo.position_blocked",
                "critical",
                f"파생 포지션 unknown·exit_failed {len(bad)}건 — 사람이 adopt·close 로 정리",
                link="/fo",
                count=len(bad),
                extra={"ids": [int(r["id"]) for r in bad]},
            )
        )
    settling = [
        r
        for r in rows
        if r["status"] == "awaiting_settlement"
        and r["last_trade_date"]
        and (today - date.fromisoformat(r["last_trade_date"])).days > FO_SETTLE_GRACE_DAYS
    ]
    if settling:
        out.append(
            Item(
                "fo.settlement_overdue",
                "warning",
                f"파생 정산 대기 {len(settling)}건이 최종거래일 뒤 "
                f"{FO_SETTLE_GRACE_DAYS}운영일을 넘김",
                link="/fo",
                count=len(settling),
            )
        )
    sell_near = [
        r
        for r in rows
        if r["vehicle"] == "OPTION_SELL"
        and r["status"] in ("held", "resizing", "entering")
        and r["last_trade_date"]
        and (date.fromisoformat(r["last_trade_date"]) - today).days <= 1
    ]
    if sell_near:
        out.append(
            Item(
                "fo.option_sell_expiry",
                "critical",
                f"옵션 매도 {len(sell_near)}건이 최종거래일 D-1 안인데 미청산",
                link="/fo",
                count=len(sell_near),
            )
        )
    local = now.astimezone(MARKET_TZ["KR"])
    lo, _ = repo.day_bounds(today, "KR")
    closed = local.strftime("%H:%M") >= fo_close_hm(False)
    bound = lo if not closed else repo.day_bounds(today + timedelta(days=1), "KR")[0]
    stale = int(
        conn.execute(
            f"SELECT COUNT(*) FROM orders WHERE asset='fo' AND account_id IN ({marks}) "
            f"AND created_at < ? AND status NOT IN ({dead_status_sql()},'filled')",
            [*ids, bound],
        ).fetchone()[0]
    )
    if stale:
        out.append(
            Item(
                "fo.orders_stale",
                "watch",
                f"파생 마감(15:45) 뒤에도 정리되지 않은 주문 {stale}건 (체결 동기화·대조 확인)",
                link="/orders",
                count=stale,
            )
        )
    return out
