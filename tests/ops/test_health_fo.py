"""Phase 26 단계 2 Task 2.7: 운영 점검 `fo` 영역 + 현물 `orders` 집계는 파생 주문을 세지 않는다."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from conftest import make_account

from trader.config import AccountConfig, Settings
from trader.fo import store
from trader.ledger import repo
from trader.ops import health

NOW = datetime(2026, 10, 23, 8, 30, tzinfo=UTC)  # 17:30 KST 금요일
MID = datetime(2026, 10, 23, 1, 0, tzinfo=UTC)  # 10:00 KST
ACCT = "paper-fo"


def fo_account() -> AccountConfig:
    return AccountConfig(
        id=ACCT, env="paper", cano="00000000", acnt_prdt_cd="03", markets=["KR"], mode="auto",
        asset="fo",
    )  # fmt: skip


def _pos(conn, *, status, vehicle="FUTURES", last_trade_date=None, side="LONG"):
    with store.tx(conn):
        return store.insert_position(
            conn, account_id=ACCT, code="105W11", kind="FUTURES", contract_side=side,
            direction="LONG", vehicle=vehicle, status=status, qty_target=1, qty_held=1,
            last_trade_date=last_trade_date,
        )  # fmt: skip


def _order(conn, *, asset, status, account_id, created_at=MID):
    return repo.insert_order(
        conn, decision_id=None, account_id=account_id, ticker="105W11", market="KR", side="BUY",
        qty=1, order_type="LIMIT", price=Decimal("350"), status=status, order_no="1", message="",
        mode="auto", asset=asset, created_at=created_at.isoformat(),
    )  # fmt: skip


def _check(conn, project, accounts):
    return health.check(conn, Settings(), accounts, now=NOW, state=project / "state")


def test_health_fo_area_ok_without_fo_accounts(conn, project):
    r = _check(conn, project, [make_account("paper-main", "paper", "auto", ["KR"])])
    assert r["areas"]["fo"] == "ok" and not [i for i in r["items"] if i["key"].startswith("fo.")]


def test_health_fo_area_levels(conn, project):
    acct = fo_account()
    r = _check(conn, project, [acct])
    assert r["areas"]["fo"] == "ok"
    # 정산 대기 1운영일 초과 → warning
    _pos(conn, status="awaiting_settlement", last_trade_date="2026-10-21")
    conn.commit()
    r = _check(conn, project, [acct])
    assert r["areas"]["fo"] == "warning" and "fo.settlement_overdue" in {
        i["key"] for i in r["items"]
    }
    # 옵션 매도 D-1 미청산 → critical
    _pos(conn, status="held", vehicle="OPTION_SELL", last_trade_date="2026-10-24", side="SHORT")
    conn.commit()
    r = _check(conn, project, [acct])
    assert r["areas"]["fo"] == "critical"
    assert "fo.option_sell_expiry" in {i["key"] for i in r["items"]}
    # unknown·exit_failed → critical (ids 포함)
    pid = _pos(conn, status="unknown")
    conn.commit()
    r = _check(conn, project, [acct])
    blocked = next(i for i in r["items"] if i["key"] == "fo.position_blocked")
    assert blocked["level"] == "critical" and blocked["extra"]["ids"] == [pid]


def test_health_fo_stale_orders_use_fo_clock(conn, project):
    """15:45 뒤 살아 있는 파생 주문은 fo 영역 watch 이고, 현물 orders 영역은 파생 주문을
    unknown·stale 로 세지 않는다."""
    acct = fo_account()
    stock = make_account("paper-main", "paper", "auto", ["KR"])
    _order(conn, asset="fo", status="accepted", account_id=ACCT)
    _order(conn, asset="fo", status="unknown", account_id=ACCT)
    conn.commit()
    r = _check(conn, project, [acct, stock])
    stale = next(i for i in r["items"] if i["key"] == "fo.orders_stale")
    assert stale["level"] == "watch" and stale["count"] == 2
    assert r["areas"]["orders"] == "ok", [i for i in r["items"] if i["key"].startswith("orders")]
    # 같은 모양의 현물 주문은 orders 영역에 잡힌다
    _order(conn, asset="stock", status="unknown", account_id="paper-main")
    conn.commit()
    r = _check(conn, project, [acct, stock])
    assert r["areas"]["orders"] != "ok"
