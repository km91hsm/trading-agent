"""파생 계좌 잔고 갱신·대조(Phase 26 단계 2 Task 2.7). `orchestrator/balance.refresh` 와
`orchestrator/reconcile.reconcile_all` 이 `account.asset == "fo"` 계좌를 여기로 보낸다.

- `refresh`: 브로커 잔고 → `fo_nav_snapshot`(포지션 없어도 매번 NAV·현금) + `fo_positions_snapshot`.
- `reconcile`: `positions.recover` 로 원장 포지션·다리 vs 브로커 잔고·체결을 대조하고 unknown·
  미확정이 생기면 알림 1건 + `control.reconcile:<계좌>:KR:<날짜>`(현물과 같은 키·문구).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from trader.config import KST, AccountConfig, FoCfg
from trader.control import control
from trader.fo import positions, store
from trader.notify import messages

logger = logging.getLogger("trader.fo.balance")


def snapshot_age(conn: sqlite3.Connection, account_id: str, now: datetime) -> float:
    """마지막 `fo_nav_snapshot` 이후 초. 없으면 무한대."""
    row = conn.execute(
        "SELECT created_at FROM fo_nav_snapshot WHERE account_id = ? ORDER BY date DESC LIMIT 1",
        (account_id,),
    ).fetchone()
    if row is None:
        return float("inf")
    return (now - datetime.fromisoformat(row["created_at"])).total_seconds()


def refresh(
    conn: sqlite3.Connection,
    broker: Any,
    account: AccountConfig,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    cfg: FoCfg,
    balance: Any | None = None,
) -> dict:
    """`balance` 를 주면(같은 틱에 이미 읽은 잔고) 다시 조회하지 않는다."""
    bal = balance if balance is not None else broker.get_balance()
    t = now()
    today = t.astimezone(KST).date().isoformat()
    multiplier = cfg.product_cfg().multiplier
    rows = []
    for p in bal.positions:
        sign = 1 if p.contract_side == "LONG" else -1
        rows.append(
            {
                "code": p.code,
                "kind": p.kind,
                "contract_side": p.contract_side,
                "qty": p.qty,
                "avg": p.avg_price,
                "current": p.current_price,
                "delta": None,
                "delta_notional": sign * p.qty * p.current_price * multiplier
                if p.kind == "FUTURES"
                else None,
                "eval": p.pnl,
            }  # fmt: skip
        )
    with store.tx(conn):
        store.write_nav_snapshot(
            conn, account_id=account.id, date=today, nav=bal.cash.nav,
            cash=bal.cash.orderable_cash, margin_total=bal.cash.margin_total,
            add_margin=bal.cash.add_margin,
        )  # fmt: skip
        store.write_positions_snapshot(conn, account_id=account.id, date=today, rows=rows)
    return {
        "account_id": account.id, "date": today, "nav": str(bal.cash.nav) if bal.cash.nav else None,
        "positions": len(rows), "unverified": len(bal.unverified),
        "requested_at": bal.cash.requested_at.isoformat(),
    }  # fmt: skip


def reconcile(
    conn: sqlite3.Connection,
    broker: Any,
    account: AccountConfig,
    *,
    today: date,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    notifier: Any | None = None,
) -> dict:
    """브로커가 진실: `positions.recover` 가 원장을 맞추고, 불일치(unknown·미확정 잔고)가 있으면
    알림 1건 + control 플래그(현물 `reconcile:<계좌>:<시장>:<날짜>` 와 같은 키)."""
    bal = broker.get_balance()
    rep = positions.recover(
        conn, account_id=account.id, balance=bal, order_state_of=broker.get_order_state,
        fills_of=broker.get_fills, today=today, now=now(),
    )  # fmt: skip
    conn.commit()
    parts: list[str] = []
    if rep.unknown:
        parts.append(f"unknown 포지션 {len(rep.unknown)}건: {rep.unknown}")
    if rep.unverified:
        parts.append(f"미확정 잔고 행 {len(rep.unverified)}건: {rep.unverified}")
    out = {
        "account_id": account.id, "market": "KR", "date": today.isoformat(),
        "adopted": rep.adopted, "unknown": rep.unknown, "unverified": rep.unverified,
        "settling": rep.settling, "legs_fixed": rep.legs_fixed, "mismatches": parts,
        "nav": str(bal.cash.nav) if bal.cash.nav is not None else None,
    }  # fmt: skip
    if parts:
        control.set_flag(conn, f"reconcile:{account.id}:KR:{today.isoformat()}", out)
        if notifier is not None:
            try:
                notifier.send(
                    messages.reconcile_mismatch(
                        account=account.id, market="KR(파생)", day=today.isoformat(), parts=parts
                    )
                )
            except Exception:  # noqa: BLE001 — 알림 실패가 대조를 바꾸지 않는다
                logger.debug("파생 대조 알림 실패", exc_info=True)
    for text in rep.alerts:
        if notifier is not None:
            try:
                notifier.send(text)
            except Exception:  # noqa: BLE001
                logger.debug("파생 복구 알림 실패", exc_info=True)
    logger.info("파생 대조 %s: 불일치 %d, 승계 %d", account.id, len(parts), len(rep.adopted))
    return out


__all__ = ["Decimal", "refresh", "reconcile", "snapshot_age"]
