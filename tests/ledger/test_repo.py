from datetime import date
from decimal import Decimal

from helpers_agents import ok_decision, ok_report, ok_turn

from trader.ledger import db, repo

D = date(2026, 10, 9)


def _tables(c):
    return {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_v2_migration_adds_tables(conn):
    assert db.SCHEMA_VERSION == 2 and db.current_version(conn) == 2
    assert {
        "analyst_reports",
        "debate_turns",
        "decisions",
        "risk_checks",
        "orders",
        "fills",
        "positions_snapshot",
        "approvals",
    } <= _tables(conn)
    db.init_schema(conn)  # 멱등
    assert db.current_version(conn) == 2


def test_ingest_checkpoint_idempotent(conn):
    for _ in range(2):
        repo.ingest_checkpoint(
            conn, D, "KR", "005930", "10_analyst_technical", {"data": ok_report(), "call": None}
        )
        repo.ingest_checkpoint(
            conn,
            D,
            "KR",
            "005930",
            "20_debate_r1_bear",
            {"data": ok_turn("bear", round=1), "call": None},
        )
        repo.ingest_checkpoint(
            conn,
            D,
            "KR",
            "005930",
            "30_decision",
            {"data": ok_decision(), "call": {"session_id": "s1"}},
        )
    assert conn.execute("SELECT COUNT(*) FROM analyst_reports").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM debate_turns").fetchone()[0] == 1
    rows = conn.execute("SELECT action, session_id, ticker FROM decisions").fetchall()
    assert len(rows) == 1 and rows[0]["action"] == "BUY" and rows[0]["session_id"] == "s1"
    assert (
        repo.ingest_checkpoint(conn, D, "KR", "005930", "25_research_summary", {"data": {}}) is None
    )
    dec_id = repo.decision_id(conn, D, "KR", "005930")
    assert isinstance(dec_id, int)


def test_orders_today_and_weekly_counts(conn):
    dec_id = repo.ingest_checkpoint(
        conn, D, "KR", "005930", "30_decision", {"data": ok_decision(), "call": None}
    )
    oid = repo.insert_order(
        conn,
        decision_id=dec_id,
        account_id="paper-main",
        ticker="005930",
        market="KR",
        side="BUY",
        qty=3,
        order_type="LIMIT",
        price=Decimal("71000"),
        status="accepted",
        order_no="0001",
        message="ok",
        mode="auto",
    )
    assert isinstance(oid, int)
    assert repo.today_buy_tickers(conn, "paper-main", D) == {"005930"}
    assert repo.today_order_amount(conn, "paper-main", D) == Decimal("213000")
    assert repo.trades_this_week(conn, "paper-main", D) == {"005930": 1}
    assert repo.today_buy_tickers(conn, "real-main", D) == set()


def test_realized_pnl_5d_and_held_since(conn):
    dec_id = repo.ingest_checkpoint(
        conn, D, "KR", "005930", "30_decision", {"data": ok_decision(), "call": None}
    )
    buy = repo.insert_order(
        conn,
        decision_id=dec_id,
        account_id="paper-main",
        ticker="005930",
        market="KR",
        side="BUY",
        qty=2,
        order_type="LIMIT",
        price=Decimal("100"),
        status="accepted",
        order_no="1",
        message="",
        mode="auto",
        created_at="2026-10-01T01:00:00+00:00",
    )
    repo.insert_fill(
        conn,
        order_id=buy,
        account_id="paper-main",
        qty=2,
        price=Decimal("100"),
        filled_at="2026-10-01T01:00:00+00:00",
    )
    sell = repo.insert_order(
        conn,
        decision_id=dec_id,
        account_id="paper-main",
        ticker="005930",
        market="KR",
        side="SELL",
        qty=2,
        order_type="LIMIT",
        price=Decimal("90"),
        status="accepted",
        order_no="2",
        message="",
        mode="auto",
        created_at="2026-10-08T01:00:00+00:00",
    )
    repo.insert_fill(
        conn,
        order_id=sell,
        account_id="paper-main",
        qty=2,
        price=Decimal("90"),
        filled_at="2026-10-08T01:00:00+00:00",
    )
    assert repo.realized_pnl_5d(conn, "paper-main", D) == Decimal("-20")
    assert repo.held_since(conn, "paper-main") == {"005930": date(2026, 10, 1)}


def test_positions_snapshot_and_risk_check(conn):
    repo.snapshot_positions(
        conn,
        account_id="paper-main",
        d=D,
        nav=Decimal("1000000"),
        cash=Decimal("500000"),
        positions=[
            {
                "ticker": "005930",
                "market": "KR",
                "qty": 5,
                "avg_price": "70000",
                "current_price": "71500",
                "eval_amount": "357500",
            }
        ],
    )
    row = conn.execute("SELECT nav, cash, qty FROM positions_snapshot").fetchone()
    assert (row["nav"], row["cash"], row["qty"]) == ("1000000", "500000", 5)
    dec_id = repo.ingest_checkpoint(
        conn, D, "KR", "005930", "30_decision", {"data": ok_decision(), "call": None}
    )
    repo.insert_risk_check(
        conn,
        decision_id=dec_id,
        account_id="paper-main",
        approved=False,
        adjusted_qty=0,
        reasons=["일일 손실 초과"],
    )
    r = conn.execute("SELECT approved, reasons FROM risk_checks").fetchone()
    assert r["approved"] == 0 and "일일 손실" in r["reasons"]


def test_approvals_lifecycle(conn):
    dec_id = repo.ingest_checkpoint(
        conn, D, "KR", "005930", "30_decision", {"data": ok_decision(), "call": None}
    )
    aid = repo.create_approval(
        conn,
        decision_id=dec_id,
        account_id="real-main",
        order={"ticker": "005930", "qty": 1},
        requested_at="2026-10-09T06:00:00+00:00",
        expires_at="2026-10-09T06:30:00+00:00",
    )
    pending = repo.pending_approvals(conn)
    assert len(pending) == 1 and pending[0]["id"] == aid and pending[0]["order"]["qty"] == 1
    repo.decide_approval(conn, aid, "approved", decided_at="2026-10-09T06:10:00+00:00")
    assert repo.pending_approvals(conn) == []
    assert repo.get_approval(conn, aid)["status"] == "approved"
