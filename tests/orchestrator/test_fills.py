from datetime import UTC, date, datetime
from decimal import Decimal

from conftest import make_account
from helpers_agents import ok_decision

from trader.broker.fake import FakeBroker
from trader.broker.models import Fill
from trader.ledger import repo
from trader.orchestrator import fills as fills_mod

D = date(2026, 10, 9)


def _fill(order_no, ticker="005930", qty=3, price="70900", side="BUY"):
    return Fill(
        order_no=order_no,
        ticker=ticker,
        market="KR",
        side=side,
        qty=qty,
        price=Decimal(price),
        filled_at=datetime(2026, 10, 9, 1, 5, 12, tzinfo=UTC),
    )


def _order(conn, order_no, qty=3):
    dec_id = repo.ingest_checkpoint(conn, D, "KR", "005930", "30_decision", {"data": ok_decision()})
    return repo.insert_order(
        conn,
        decision_id=dec_id,
        account_id="paper-main",
        ticker="005930",
        market="KR",
        side="BUY",
        qty=qty,
        order_type="LIMIT",
        price=Decimal("71000"),
        status="accepted",
        order_no=order_no,
        message="",
        mode="auto",
    )


def test_sync_fills_idempotent(conn):
    acct = make_account("paper-main", "paper", "auto")
    broker = FakeBroker(acct, fills={("KR", D): [_fill("0000117057")]})
    _order(conn, "0000117057")
    r1 = fills_mod.sync_fills(conn, broker, acct, D)
    r2 = fills_mod.sync_fills(conn, broker, acct, D)
    assert (r1["inserted"], r2["inserted"]) == (1, 0)
    assert conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1


def test_sync_marks_filled_and_partial(conn):
    acct = make_account("paper-main", "paper", "auto")
    oid_full = _order(conn, "A", qty=3)
    oid_part = _order(conn, "B", qty=5)
    broker = FakeBroker(acct, fills={("KR", D): [_fill("A", qty=3), _fill("B", qty=2)]})
    r = fills_mod.sync_fills(conn, broker, acct, D)
    assert r["inserted"] == 2 and r["orders_updated"] == 2
    rows = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM orders")}
    assert rows[oid_full] == "filled" and rows[oid_part] == "partial"


def test_external_fill_creates_order_row(conn):
    acct = make_account("paper-main", "paper", "auto")
    broker = FakeBroker(acct, fills={("KR", D): [_fill("ZZZ", ticker="000660", side="SELL")]})
    r = fills_mod.sync_fills(conn, broker, acct, D)
    assert r["inserted"] == 1
    o = conn.execute("SELECT * FROM orders WHERE order_no='ZZZ'").fetchone()
    assert o["mode"] == "external" and o["decision_id"] is None and o["status"] == "filled"
    assert o["side"] == "SELL" and o["ticker"] == "000660"
    assert repo.held_since(conn, "paper-main") == {}  # 매도 체결은 보유 시작이 아니다


def test_sync_skips_markets_not_in_account(conn):
    acct = make_account("paper-main", "paper", "auto", ["KR"])
    broker = FakeBroker(acct, fills={("US", D): [_fill("X")]})
    r = fills_mod.sync_fills(conn, broker, acct, D)
    assert r["inserted"] == 0 and "get_fills" in broker.calls
