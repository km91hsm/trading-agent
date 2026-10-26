"""누적 손실 킬스위치(Stock D-049·D-059 교훈): 일일 손실 한도(−5%)는 매일 리셋돼 며칠에 걸친
누적 하락을 막지 못한다 — (계좌·시장) NAV 가 기준 대비 `risk.killswitch_drawdown_pct`(15%) 넘게
빠지면 `control.killswitch` 를 켜고, 켜져 있는 동안 게이트가 일반 BUY/ADD·단타 진입을 거부한다
(매도·손절·헤지는 그대로 — 위험을 줄이는 주문은 막지 않는다).

기준은 그 계좌·시장의 첫 잔고 스냅샷 NAV(시작 자산)이고, 해제하면 해제 시점의 NAV 로 다시 잡는다
(같은 손실로 곧바로 다시 켜지지 않게). **해제는 사람만**(`trader control killswitch off --note`,
원인 메모 필수) — 어떤 자동 경로도 끄지 않는다. 텔레그램 `/kill` 은 켜기만 한다.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from trader.control import control

FLAG = "killswitch"
MIN_NOTE = 10  # 해제 메모 최소 글자 수(원인을 적게 한다)


def _base_key(account_id: str, market: str) -> str:
    return f"killswitch_base:{account_id}:{market}"


def state(conn: sqlite3.Connection) -> dict:
    return control.get_flag(conn, FLAG, {}) or {}


def reason(conn: sqlite3.Connection) -> str | None:
    """켜져 있으면 사유, 아니면 None."""
    st = state(conn)
    return str(st.get("reason") or "킬스위치") if st.get("on") else None


def _dec(raw) -> Decimal | None:
    try:
        v = Decimal(str(raw)) if raw is not None else None
    except InvalidOperation:
        return None
    return v if v is not None and v > 0 else None


def _baseline(
    conn: sqlite3.Connection, account_id: str, market: str, nav: Decimal
) -> Decimal | None:
    """기준 NAV. 처음 보는 (계좌·시장)이면 **한 번 저장**한다 — 스냅샷은 하루 1행 upsert 라 첫날
    값이 장 마감 NAV 로 덮여 첫날 손실이 기준에서 사라진다(Codex 리뷰). 저장 전 기존 운영분은
    가장 이른 양수 스냅샷 NAV(0 이 먼저 있어도 건너뛴다), 없으면 지금 NAV."""
    key = _base_key(account_id, market)
    stored = _dec((control.get_flag(conn, key) or {}).get("nav"))
    if stored is not None:
        return stored
    first = None
    for (raw,) in conn.execute(
        "SELECT nav FROM positions_snapshot WHERE account_id=? AND market=? "
        "AND nav IS NOT NULL ORDER BY date, id",
        (account_id, market),
    ):
        first = _dec(raw)
        if first is not None:
            break
    base = first if first is not None else _dec(nav)
    if base is not None:
        control.set_flag(conn, key, {"nav": str(base), "at": datetime.now(UTC).isoformat()})
    return base


def engage(conn: sqlite3.Connection, *, reason: str, source: str) -> None:
    control.set_flag(
        conn,
        FLAG,
        {"on": True, "reason": reason, "source": source, "at": datetime.now(UTC).isoformat()},
    )


def observe(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    market: str,
    nav: Decimal,
    today: date,
    limit_pct: float,
) -> bool:
    """잔고를 볼 때마다 부른다. 이번에 새로 켰으면 True(알림은 호출부·운영 점검이 한다)."""
    if limit_pct <= 0 or nav <= 0:
        return False
    base = _baseline(conn, account_id, market, nav)  # 켜져 있어도 기준은 처음에 한 번 남긴다
    if base is None or state(conn).get("on"):
        return False
    dd = float(nav / base - 1)
    if dd > -limit_pct:
        return False
    engage(
        conn,
        reason=(
            f"{account_id} {market} 누적 손실 {dd:.1%} (기준 {base} → {nav}, "
            f"한도 -{limit_pct:.0%}, {today.isoformat()})"
        ),
        source="auto",
    )
    return True


def release(conn: sqlite3.Connection, *, note: str) -> None:
    """사람만 부른다. 원인 메모가 필요하고, 모든 계좌·시장의 기준을 최근 NAV 로 다시 잡는다."""
    if len(note.strip()) < MIN_NOTE:
        raise ValueError(f"해제 메모는 {MIN_NOTE}자 이상 원인을 적어야 합니다")
    rows = conn.execute(
        "SELECT account_id, market, nav FROM positions_snapshot p WHERE id = ("
        "SELECT id FROM positions_snapshot q WHERE q.account_id=p.account_id "
        "AND q.market=p.market AND q.nav IS NOT NULL ORDER BY date DESC, id DESC LIMIT 1)"
    ).fetchall()
    now = datetime.now(UTC).isoformat()
    for acct, market, nav in rows:
        control.set_flag(conn, _base_key(acct, market), {"nav": str(nav), "at": now})
    prev = state(conn)
    control.set_flag(
        conn,
        FLAG,
        {"on": False, "released_note": note.strip(), "released_at": now, "last": prev},
    )
