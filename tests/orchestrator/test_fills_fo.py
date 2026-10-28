"""Phase 26 단계 2 Task 2.7: 파생 체결 동기화 — 계보 누적 교체·증분 평균가, 주문번호 없는
다리 매칭(정확히 1건), 조회 실패 시 만료 없음, 15:45 뒤 만료, 현물 `asset` 기본값."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from trader.broker.fake import FakeFoBroker
from trader.broker.models import FoBalance, FoCash, FoFill
from trader.config import AccountConfig, FoCfg
from trader.fo import fills as fo_fills
from trader.fo import store
from trader.ledger import repo
from trader.orchestrator import fills as fills_mod

ACCT = "paper-fo"
DAY = date(2026, 10, 27)
T_MID = datetime(2026, 10, 27, 1, 30, tzinfo=UTC)  # 10:30 KST
T_LATE = datetime(2026, 10, 27, 7, 0, tzinfo=UTC)  # 16:00 KST
CFG = FoCfg(enabled=True)
MULT = CFG.product_cfg().multiplier


def account(asset="fo") -> AccountConfig:
    return AccountConfig(
        id=ACCT, env="paper", cano="00000000", acnt_prdt_cd="03", markets=["KR"], mode="auto",
        asset=asset,
    )  # fmt: skip


def balance() -> FoBalance:
    z = Decimal(0)
    cash = FoCash(
        deposit=z, orderable_cash=z, orderable_total=z, margin_total=z, add_margin=z, nav=None,
        realized_today=z, unrealized=z, requested_at=T_MID, received_at=T_MID,
    )  # fmt: skip
    return FoBalance(positions=[], unverified=[], cash=cash)


def fill(order_no, *, filled, amount, remaining, orgn=None, side="BUY", code="105W11"):
    """`amount` 는 포인트 합(가격 × 수량); KIS 체결금액(통화)은 승수를 곱한 값이다."""
    return FoFill(
        order_no=order_no, orgn_order_no=orgn, code=code, side=side, remaining_qty=remaining,
        filled_qty=filled, rejected_qty=0, cancelled_qty=0,
        filled_amount=Decimal(amount) * MULT,  # 통화 금액(포인트 × 승수)
        avg_price=None, filled_at=T_MID,
    )  # fmt: skip


def position(conn, *, status="entering", qty_held=0, side="LONG") -> int:
    with store.tx(conn):
        return store.insert_position(
            conn, account_id=ACCT, code="105W11", kind="FUTURES", contract_side=side,
            direction="LONG" if side == "LONG" else "SHORT", vehicle="FUTURES", status=status,
            qty_target=4, qty_held=qty_held, nav_at_open=Decimal("100000000"),
        )  # fmt: skip


def leg(conn, pid, *, status="accepted", order_no="A", qty=4, intent="OPEN", with_order=True):
    with store.tx(conn):
        lid = store.insert_leg(
            conn, position_id=pid, account_id=ACCT, intent=intent, round=0, attempt=1, qty=qty,
            price=Decimal("350.5"), order_type="LIMIT",
        )  # fmt: skip
        oid = None
        if with_order:
            oid = repo.insert_order(
                conn, decision_id=None, account_id=ACCT, ticker="105W11", market="KR",
                side="BUY" if intent == "OPEN" else "SELL", qty=qty, order_type="LIMIT",
                price=Decimal("350.5"), status="accepted", order_no=order_no, message="",
                mode="auto", asset="fo", created_at=T_MID.isoformat(),
            )  # fmt: skip
        store.set_leg(
            conn, lid, status=status, order_no=order_no, order_id=oid,
            sent_at=T_MID.isoformat(), accepted_at=T_MID.isoformat() if order_no else None,
        )  # fmt: skip
    return lid


def test_fo_fills_cumulative_replace_and_increment_price(conn):
    """같은 계보를 두 번 동기화해도 fills 행은 1개(누적값 교체)이고, 평균가는 Δ금액÷Δ수량의
    증분으로 수량 가중된다."""
    pid = position(conn)
    lid = leg(conn, pid)
    broker = FakeFoBroker(account(), balance=balance(), now=lambda: T_MID)
    broker.fills = {DAY: [fill("A", filled=2, amount="701.0", remaining=2)]}
    r1 = fo_fills.sync(conn, broker, account(), DAY, cfg=CFG, now=lambda: T_MID)
    assert (r1["inserted"], r1["expired"]) == (1, 0)
    pos = store.get_position(conn, pid)
    assert (pos.qty_held, pos.entry_premium) == (2, Decimal("350.5"))
    # 둘째 조회: 누적 4계약·금액 1403 → 증분 2계약 @351
    broker.fills = {DAY: [fill("A", filled=4, amount="1403.0", remaining=0)]}
    r2 = fo_fills.sync(conn, broker, account(), DAY, cfg=CFG, now=lambda: T_MID)
    assert r2["inserted"] == 1
    rows = conn.execute("SELECT qty, price FROM fills ORDER BY id").fetchall()
    assert len(rows) == 1 and int(rows[0]["qty"]) == 4
    assert Decimal(rows[0]["price"]) == Decimal("1403.0") / 4
    pos = store.get_position(conn, pid)
    assert (pos.qty_held, pos.entry_premium) == (4, Decimal("350.75"))
    assert pos.fees == Decimal("350.5") * 2 * MULT * Decimal("0.3") / 20000 + (
        Decimal("351") * 2 * MULT * Decimal("0.3") / 20000
    )
    assert store.get_leg(conn, lid).status == "filled"
    order = conn.execute("SELECT status FROM orders").fetchone()
    assert order["status"] == "filled"
    # 세 번째 조회(변화 없음)는 아무것도 바꾸지 않는다
    r3 = fo_fills.sync(conn, broker, account(), DAY, cfg=CFG, now=lambda: T_MID)
    assert (r3["inserted"], r3["updated"]) == (0, 0)


def test_fo_fills_reduce_leg_adds_realized_loss(conn):
    """축소 다리 체결은 평균가 대비 손실만 `realized_loss` 에 더한다(이익은 0)."""
    pid = position(conn, status="closing", qty_held=2)
    with store.tx(conn):
        cur = store.get_position(conn, pid)
        store.transition(conn, pid, expected_version=cur.version, entry_premium=Decimal("350"))
    leg(conn, pid, order_no="C", qty=2, intent="CLOSE")
    broker = FakeFoBroker(account(), balance=balance(), now=lambda: T_MID)
    broker.fills = {DAY: [fill("C", filled=2, amount="698.0", remaining=0, side="SELL")]}
    fo_fills.sync(conn, broker, account(), DAY, cfg=CFG, now=lambda: T_MID)
    pos = store.get_position(conn, pid)
    assert pos.qty_held == 0 and pos.realized_loss == Decimal("1") * 2 * MULT


def test_fo_fills_match_sending_leg_exactly_one(conn):
    """주문번호 없는 sending 다리는 같은 코드·방향·수량 후보가 정확히 1건일 때만 붙고(orders 행
    생성·접수번호 확정), 후보가 둘이면 external 로 적재한다."""
    pid = position(conn)
    lid = leg(conn, pid, status="sending", order_no=None, with_order=False)
    broker = FakeFoBroker(account(), balance=balance(), now=lambda: T_MID)
    broker.fills = {DAY: [fill("X1", filled=1, amount="350.5", remaining=3)]}
    r = fo_fills.sync(conn, broker, account(), DAY, cfg=CFG, now=lambda: T_MID)
    assert (r["bound"], r["external"]) == (1, 0)
    cur = store.get_leg(conn, lid)
    assert (cur.status, cur.order_no, cur.filled_qty) == ("accepted", "X1", 1)
    o = conn.execute("SELECT mode, asset, order_no FROM orders").fetchone()
    assert (o["mode"], o["asset"], o["order_no"]) == ("recovered", "fo", "X1")
    # 후보 둘(두 포지션의 sending 다리) → 모호 → external
    pid2 = position(conn)
    pid3 = position(conn)
    leg(conn, pid2, status="sending", order_no=None, with_order=False)
    leg(conn, pid3, status="sending", order_no=None, with_order=False)
    broker.fills = {DAY: [fill("X2", filled=4, amount="1402.0", remaining=0)]}
    r = fo_fills.sync(conn, broker, account(), DAY, cfg=CFG, now=lambda: T_MID)
    assert r["external"] == 1
    assert conn.execute("SELECT COUNT(*) FROM orders WHERE mode='external'").fetchone()[0] == 1
    assert all(
        store.get_leg(conn, x.id).status == "sending"
        for x in store.live_legs(conn, account_id=ACCT)
        if x.position_id in (pid2, pid3)
    )


def test_fo_fills_no_expire_on_query_failure(conn):
    """체결 조회가 실패하면 마감 뒤여도 accepted 다리를 만료시키지 않는다."""
    pid = position(conn)
    lid = leg(conn, pid)
    broker = FakeFoBroker(account(), balance=balance(), fail={"get_fills"}, now=lambda: T_LATE)
    r = fo_fills.sync(conn, broker, account(), DAY, cfg=CFG, now=lambda: T_LATE)
    assert r["failed_markets"] == ["KR"] and r["expired"] == 0
    assert store.get_leg(conn, lid).status == "accepted"


def test_fo_fills_expire_after_1545(conn):
    """조회 성공 ∧ 15:45 KST 뒤 → 그날 체결 없는 accepted 다리·주문은 expired. 장중은 그대로."""
    pid = position(conn)
    lid = leg(conn, pid)
    broker = FakeFoBroker(account(), balance=balance(), now=lambda: T_MID)
    r = fo_fills.sync(conn, broker, account(), DAY, cfg=CFG, now=lambda: T_MID)
    assert r["expired"] == 0 and store.get_leg(conn, lid).status == "accepted"
    r = fo_fills.sync(conn, broker, account(), DAY, cfg=CFG, now=lambda: T_LATE)
    assert r["expired"] == 1
    assert store.get_leg(conn, lid).status == "expired"
    assert conn.execute("SELECT status FROM orders").fetchone()["status"] == "expired"


def test_sync_fills_dispatches_fo_account(conn):
    """`orchestrator.fills.sync_fills` 는 fo 계좌를 파생 동기화로 보낸다."""
    broker = FakeFoBroker(account(), balance=balance(), now=lambda: T_MID)
    r = fills_mod.sync_fills(conn, broker, account(), DAY, now=lambda: T_MID)
    assert r["account_id"] == ACCT and "bound" in r and broker.calls == ["get_fills"]


def test_stock_insert_order_asset_default_unchanged(conn):
    oid = repo.insert_order(
        conn, decision_id=None, account_id="paper-main", ticker="005930", market="KR",
        side="BUY", qty=1, order_type="LIMIT", price=Decimal("70000"), status="accepted",
        order_no="1", message="", mode="auto",
    )  # fmt: skip
    assert (
        conn.execute("SELECT asset FROM orders WHERE id=?", (oid,)).fetchone()["asset"] == "stock"
    )


# --- 단계 2 리뷰 반영(S2-1·R-1·C2-2·C2-6) ----------------------------------------------------


def test_fo_fills_partial_then_more_keeps_transaction(conn):
    """partial 뒤 같은 상태로 증가(2→3)해도 트랜잭션이 깨지지 않는다(commit 하는 헬퍼 금지,
    S2-1)."""
    pid = position(conn)
    leg(conn, pid)
    broker = FakeFoBroker(account(), balance=balance(), now=lambda: T_MID)
    broker.fills = {DAY: [fill("A", filled=2, amount="701.0", remaining=2)]}
    fo_fills.sync(conn, broker, account(), DAY, cfg=CFG, now=lambda: T_MID)
    broker.fills = {DAY: [fill("A", filled=3, amount="1052.0", remaining=1)]}
    r = fo_fills.sync(conn, broker, account(), DAY, cfg=CFG, now=lambda: T_MID)
    assert r["inserted"] == 1 and store.get_position(conn, pid).qty_held == 3
    assert conn.execute("SELECT status FROM orders").fetchone()[0] == "partial"
    assert not conn.in_transaction


def test_fo_fills_order_no_reused_next_day_not_attached(conn):
    """KIS 주문번호는 날마다 재시작: 어제 다리·주문의 번호와 같은 오늘 체결은 그 행에 붙지 않고
    외부 주문으로 적는다(R-1)."""
    from datetime import timedelta

    pid = position(conn, status="held", qty_held=4)
    lid = leg(conn, pid, order_no="A", status="filled")
    yday = (T_MID - timedelta(days=1)).isoformat()
    conn.execute(
        "UPDATE fo_order_legs SET sent_at=?, accepted_at=?, created_at=? WHERE id=?",
        (yday, yday, yday, lid),
    )
    conn.execute(
        "UPDATE orders SET created_at=? WHERE id=?", (yday, store.get_leg(conn, lid).order_id)
    )
    conn.commit()
    broker = FakeFoBroker(account(), balance=balance(), now=lambda: T_MID)
    broker.fills = {DAY: [fill("A", filled=1, amount="350.0", remaining=0)]}
    r = fo_fills.sync(conn, broker, account(), DAY, cfg=CFG, now=lambda: T_MID)
    assert r["external"] == 1 and store.get_leg(conn, lid).filled_qty == 0
    assert store.get_position(conn, pid).qty_held == 4
    assert conn.execute("SELECT COUNT(*) FROM orders WHERE mode='external'").fetchone()[0] == 1


def test_fo_fills_anomalous_lineage_isolated(conn):
    """체결 누적 > 다리 수량인 계보는 그 포지션만 unknown + 집계하고 다른 계보는 계속 처리한다
    (C2-6)."""
    pid = position(conn)
    leg(conn, pid, order_no="A", qty=4)
    with store.tx(conn):
        pid2 = store.insert_position(
            conn, account_id=ACCT, code="205W11350", kind="CALL", contract_side="LONG",
            direction="LONG", vehicle="OPTION_BUY", status="entering", qty_target=1, qty_held=0,
            nav_at_open=Decimal("100000000"),
        )  # fmt: skip
        lid2 = store.insert_leg(
            conn, position_id=pid2, account_id=ACCT, intent="OPEN", round=0, attempt=1, qty=1,
            price=Decimal("5.00"), order_type="LIMIT",
        )  # fmt: skip
        store.set_leg(conn, lid2, status="accepted", order_no="B", sent_at=T_MID.isoformat(),
                      accepted_at=T_MID.isoformat())  # fmt: skip
    broker = FakeFoBroker(account(), balance=balance(), now=lambda: T_MID)
    broker.fills = {
        DAY: [
            fill("A", filled=5, amount="1752.5", remaining=0),
            fill("B", filled=1, amount="5.0", remaining=0, code="205W11350"),
        ]
    }
    r = fo_fills.sync(conn, broker, account(), DAY, cfg=CFG, now=lambda: T_MID)
    assert r["anomalies"] == 1
    assert store.get_position(conn, pid).status == "unknown"
    assert store.get_position(conn, pid2).qty_held == 1
    assert store.get_leg(conn, lid2).status == "filled"


def test_fo_expire_closes_empty_entering(conn):
    """만료로 끝난 진입 다리는 빈 entering 포지션을 닫는다(C2-2)."""
    pid = position(conn)
    leg(conn, pid)
    assert fo_fills.expire_legs(conn, ACCT, DAY) == 1
    assert store.get_position(conn, pid).status == "closed"
