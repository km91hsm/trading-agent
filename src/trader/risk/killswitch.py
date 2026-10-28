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
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from trader.control import control

FLAG = "killswitch"
MIN_NOTE = 10  # 해제 메모 최소 글자 수(원인을 적게 한다)
# 입출금 의심(2-O16): 직전 관측 대비 NAV 변화 중 보유 종목 가격 변화로 설명되지 않는 잔차가 직전 NAV
# 의 이 비율 이상이면 기록한다. 장중 새로 산 종목의 손익(최대 비중 25% × 하루 변동)이 잔차에 섞여도
# 넘지 않게 5%. 기준은 자동으로 바꾸지 않는다 — 잔차는 추정이고, 해제·재설정은 사람만.
FLOW_SUSPECT_PCT = Decimal("0.05")


def _obs_key(account_id: str, market: str) -> str:
    return f"killswitch_obs:{account_id}:{market}"


def _flow_key(account_id: str, market: str) -> str:
    return f"killswitch_flow:{account_id}:{market}"


def flow_suspicion(conn: sqlite3.Connection, account_id: str, market: str) -> dict | None:
    """마지막 입출금 의심 기록(없으면 None). 운영 점검 loss 영역이 읽는다."""
    return control.get_flag(conn, _flow_key(account_id, market)) or None


def _check_flow(
    conn: sqlite3.Connection,
    account_id: str,
    market: str,
    nav: Decimal,
    positions: Sequence[tuple[str, int, Decimal]],
    today: date,
) -> None:
    """직전 관측(NAV·종목별 수량·가격)과 비교해 설명되지 않는 NAV 변화를 기록한다.

    설명되는 변화 = 직전에도 지금도 보유한 종목의 `직전 수량 × 가격 변화`. 매수·매도는 현금과 평가를
    맞바꿀 뿐 NAV 를 크게 바꾸지 않는다(새로 산 종목의 체결가 대비 변동만 잔차에 섞인다)."""
    now_px = {t: (int(q), Decimal(str(p))) for t, q, p in positions}
    prev = control.get_flag(conn, _obs_key(account_id, market)) or {}
    prev_nav = _dec(prev.get("nav"))
    if prev_nav is not None:
        explained = Decimal(0)
        for t, (q0, p0) in (prev.get("px") or {}).items():
            if t in now_px:
                explained += Decimal(int(q0)) * (now_px[t][1] - Decimal(str(p0)))
        residual = (nav - prev_nav) - explained
        if abs(residual) >= FLOW_SUSPECT_PCT * prev_nav:
            control.set_flag(
                conn,
                _flow_key(account_id, market),
                {
                    "date": today.isoformat(),
                    "residual": str(residual),
                    "prev_nav": str(prev_nav),
                    "nav": str(nav),
                    "at": datetime.now(UTC).isoformat(),
                },
            )
    control.set_flag(
        conn,
        _obs_key(account_id, market),
        {
            "nav": str(nav),
            "date": today.isoformat(),
            "px": {t: [q, str(p)] for t, (q, p) in now_px.items()},
        },
    )


def _base_key(account_id: str, market: str) -> str:
    return f"killswitch_base:{account_id}:{market}"


def state(conn: sqlite3.Connection) -> dict:
    return control.get_flag(conn, FLAG, {}) or {}


def reason(conn: sqlite3.Connection, account_id: str | None = None) -> str | None:
    """켜져 있으면 사유, 아니면 None. 수동(`/kill`·CLI)은 전 계좌, 자동 발동은 **발동한 계좌만**
    (`accounts`) — 모의 계좌의 누적 손실이 실전 계좌의 신규 매수까지 막던 문제(전체 검토
    2026-10-27). account_id 가 None 이면(표시·리포트) 어느 계좌든 켜져 있으면 사유."""
    st = state(conn)
    if not st.get("on"):
        return None
    scope = st.get("accounts")
    if account_id is not None and scope is not None and account_id not in scope:
        return None
    return str(st.get("reason") or "킬스위치")


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


def engage(
    conn: sqlite3.Connection, *, reason: str, source: str, account_id: str | None = None
) -> None:
    """켠다. account_id 가 있으면(자동 발동) 그 계좌만 범위에 더하고, 없으면(수동) 전 계좌.
    읽기~쓰기를 한 쓰기 트랜잭션으로 묶는다 — 자동 발동이 읽은 직후 `/kill` 이 저장되면 계좌 한정
    상태가 전체 차단을 덮어쓸 수 있었다(Codex 리뷰). set_flag 의 commit 이 트랜잭션을 끝낸다."""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    prev = state(conn)
    scope: list[str] | None = None
    if account_id is not None:
        if prev.get("on") and prev.get("accounts") is None:
            conn.commit()  # 이미 전 계좌로 켜져 있다 — 트랜잭션만 닫는다
            return
        scope = sorted({*(prev.get("accounts") or [] if prev.get("on") else []), account_id})
        if prev.get("on"):
            reason = f"{prev.get('reason')}; {reason}"
    control.set_flag(
        conn,
        FLAG,
        {
            "on": True,
            "reason": reason,
            "source": source,
            "accounts": scope,
            "at": datetime.now(UTC).isoformat(),
        },
    )


def observe(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    market: str,
    nav: Decimal,
    today: date,
    limit_pct: float,
    positions: Sequence[tuple[str, int, Decimal]] | None = None,
) -> bool:
    """잔고를 볼 때마다 부른다. 이번에 새로 켰으면 True(알림은 호출부·운영 점검이 한다).

    `positions`((종목, 수량, 현재가))가 오면 입출금 의심도 함께 본다(2-O16, 기록만)."""
    if limit_pct <= 0 or nav <= 0:
        return False
    if positions is not None:
        _check_flow(conn, account_id, market, nav, positions, today)
    base = _baseline(conn, account_id, market, nav)  # 켜져 있어도 기준은 처음에 한 번 남긴다
    if base is None or reason(conn, account_id):  # 이 계좌는 이미 막혀 있다
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
        account_id=account_id,
    )
    return True


def release(conn: sqlite3.Connection, *, note: str) -> None:
    """사람만 부른다. 원인 메모가 필요하고, 모든 계좌·시장의 기준을 최근 NAV 로 다시 잡는다
    (입출금 의심 기록도 지운다 — 기준이 새 NAV 라 더 이상 해당 없음)."""
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
    conn.execute("DELETE FROM control WHERE key LIKE 'killswitch\\_flow:%' ESCAPE '\\'")
    prev = state(conn)
    control.set_flag(
        conn,
        FLAG,
        {"on": False, "released_note": note.strip(), "released_at": now, "last": prev},
    )
