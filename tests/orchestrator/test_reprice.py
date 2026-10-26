"""아침 미체결 재호가(Phase 23 B): 후보·추격 상한·취소→재제출·운영일 1회·dry_run/off 제외."""

from datetime import UTC, date, datetime
from decimal import Decimal

from helpers_agents import ok_decision

from trader.broker.fake import FakeBroker
from trader.broker.models import Cash
from trader.config import AccountConfig, RepriceCfg, Settings
from trader.control import control
from trader.ledger import repo
from trader.orchestrator import executor, reprice
from trader.orchestrator.run_store import read_checkpoint, run_dir

D = date(2026, 10, 23)
T0 = datetime(2026, 10, 23, 0, 40, tzinfo=UTC)  # 09:40 KST
CFG = RepriceCfg()


class Notes:
    def __init__(self):
        self.sent = []

    def send(self, text, *, escape=True):
        self.sent.append(text)
        return True


def _ctx(conn, *, paper_mode="auto"):
    accounts = [
        AccountConfig(id="paper-main", env="paper", cano="1", markets=["KR"], mode=paper_mode),
        AccountConfig(id="real-main", env="real", cano="2", markets=["KR"], mode="off"),
    ]
    cash = {"KR": Cash(currency="KRW", available=Decimal("10000000"), total=Decimal("10000000"))}
    brokers = {a.id: FakeBroker(a, cash=cash) for a in accounts}
    return executor.ExecContext(
        settings=Settings(),
        accounts=accounts,
        conn=conn,
        brokers=brokers,
        notifier=Notes(),
        now=lambda: T0,
    )


def _order(conn, *, price="70000", status="accepted", filled=0, side="BUY", account="paper-main"):
    did = repo.ingest_checkpoint(
        conn, D, "KR", "005930", "30_decision", {"data": ok_decision(limit_price=float(price))}
    )
    oid = repo.insert_order(
        conn,
        decision_id=did,
        account_id=account,
        ticker="005930",
        market="KR",
        side=side,
        qty=10,
        order_type="LIMIT",
        price=Decimal(price),
        status=status,
        order_no="0001",
        message="",
        mode="auto",
        created_at="2026-10-23T00:05:00+00:00",
        org_no="00000",
    )
    if filled:
        repo.insert_fill(
            conn,
            order_id=oid,
            account_id=account,
            qty=filled,
            price=Decimal(price),
            filled_at=T0.isoformat(),
        )
    return did, oid


def test_candidates_only_unfilled_limit_accepted_today(conn):
    _order(conn)
    _, o2 = _order(conn, filled=10)
    _, o3 = _order(conn, status="filled")
    rows = reprice.candidates(conn, "paper-main", "KR", D)
    assert [r["id"] for r in rows] == [1] and o2 != 1 and o3 != 1


def test_too_far_rule():
    assert reprice.too_far("BUY", Decimal("100"), Decimal("101.5"), 0.01)
    assert not reprice.too_far("BUY", Decimal("100"), Decimal("100.9"), 0.01)
    assert reprice.too_far("SELL", Decimal("100"), Decimal("98.9"), 0.01)
    assert not reprice.too_far("SELL", Decimal("100"), Decimal("99.5"), 0.01)


def test_reprice_cancels_and_resubmits_at_quote_once_per_day(conn):
    ctx = _ctx(conn)
    did, oid = _order(conn, price="70000")
    out = reprice.reprice(
        ctx, market="KR", today=D, cfg=CFG, quote_fn=lambda t, e: Decimal("70300")
    )
    assert out["repriced"][0]["from"] == "70000" and out["repriced"][0]["to"] == "70300"
    assert out["repriced"][0]["approved"] is True and out["repriced"][0]["new_order"] == "accepted"
    broker = ctx.brokers["paper-main"]
    assert broker.cancelled == ["0001"] and len(broker.orders) == 1
    assert broker.orders[0].price == Decimal("70300") and broker.orders[0].order_type == "LIMIT"
    rows = conn.execute("SELECT id, status, price, decision_id FROM orders ORDER BY id").fetchall()
    assert rows[0]["status"] == "cancelled" and rows[1]["status"] == "accepted"
    assert rows[1]["decision_id"] == did and rows[1]["price"] == "70300"
    d = run_dir(D, "KR", "005930")
    assert read_checkpoint(d, "42_reprice_paper-main")["data"]["approved"] is True
    assert read_checkpoint(d, "52_order_reprice_paper-main")["data"]["status"] == "accepted"
    assert read_checkpoint(d, "50_order_paper-main") is None  # 오늘 분석의 50_ 는 건드리지 않는다
    assert any("[재호가]" in m and "70,000원 → 70,300원" in m for m in ctx.notifier.sent)
    # 운영일 1회
    assert (
        reprice.reprice(ctx, market="KR", today=D, cfg=CFG, quote_fn=lambda t, e: Decimal(1))[
            "skipped"
        ]
        == "오늘 이미 실행"
    )
    assert control.get_flag(conn, reprice.done_key("KR", D)) is True


def test_reprice_skips_when_chase_too_far_or_dry_run_or_off(conn):
    ctx = _ctx(conn)
    _order(conn, price="70000")
    out = reprice.reprice(
        ctx,
        market="KR",
        today=D,
        cfg=CFG,
        quote_fn=lambda t, e: Decimal("71000"),  # +1.4%
    )
    assert out["repriced"] == [] and "불리" in out["skipped"][0]["why"]
    assert ctx.brokers["paper-main"].cancelled == []
    assert any("[재호가 보류]" in m for m in ctx.notifier.sent)
    control.set_flag(conn, reprice.done_key("KR", D), None)
    assert reprice.reprice(
        ctx, market="KR", today=D, cfg=CFG, quote_fn=lambda t, e: Decimal(1), dry_run=True
    ) == {"skipped": "dry-run"}
    assert reprice.reprice(
        ctx, market="KR", today=D, cfg=RepriceCfg(enabled=False), quote_fn=lambda t, e: Decimal(1)
    ) == {"skipped": "reprice.enabled=false"}
    # off 계좌의 주문은 건너뛴다
    conn.execute("DELETE FROM orders")
    conn.commit()
    _order(conn, price="70000", account="real-main")
    out = reprice.reprice(
        ctx, market="KR", today=D, cfg=CFG, quote_fn=lambda t, e: Decimal("70100")
    )
    assert out["repriced"] == [] and ctx.brokers["real-main"].cancelled == []


def test_candidates_exclude_stop_loss_and_hedge_orders(conn):
    """전체 검토 O2: 손절 SELL(결정은 BUY)·헤지 결정의 주문은 재호가하지 않는다."""
    _, o_buy = _order(conn)  # BUY 결정 + BUY 주문 → 후보
    _, o_sl = _order(conn, side="SELL")  # BUY 결정에 붙은 SELL = 손절 → 제외
    conn.execute("UPDATE decisions SET source='hedge'")
    conn.commit()
    assert reprice.candidates(conn, "paper-main", "KR", D) == []
    conn.execute("UPDATE decisions SET source='llm'")
    conn.commit()
    assert [r["id"] for r in reprice.candidates(conn, "paper-main", "KR", D)] == [o_buy]
    assert o_sl != o_buy
