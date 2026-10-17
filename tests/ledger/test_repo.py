from datetime import date
from decimal import Decimal

from helpers_agents import ok_decision, ok_report, ok_turn

from trader.ledger import db, repo

D = date(2026, 10, 9)


def _tables(c):
    return {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_v2_migration_adds_tables(conn):
    assert db.SCHEMA_VERSION >= 2 and db.current_version(conn) == db.SCHEMA_VERSION
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
    assert db.current_version(conn) == db.SCHEMA_VERSION


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
        created_at="2026-10-09T01:00:00+00:00",
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
    # 전량 매도로 보유가 0 이 되면 보유 시작일도 사라진다 (재진입 때 최소 보유기간이 다시 적용)
    assert repo.held_since(conn, "paper-main") == {}


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


# --- 리뷰 반영 ---------------------------------------------------------------------


def test_v3_adds_amount_and_org_no(conn):
    assert db.SCHEMA_VERSION >= 3
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(orders)")}
    assert {"amount", "org_no"} <= cols


def test_market_order_amount_uses_amount_column(conn):
    dec_id = repo.ingest_checkpoint(
        conn, D, "KR", "005930", "30_decision", {"data": ok_decision(), "call": None}
    )
    repo.insert_order(
        conn,
        decision_id=dec_id,
        account_id="paper-main",
        ticker="005930",
        market="KR",
        side="BUY",
        qty=3,
        order_type="MARKET",
        price=None,
        amount=Decimal("214500"),
        status="accepted",
        order_no="1",
        message="",
        mode="auto",
        org_no="91252",
        created_at="2026-10-09T01:00:00+00:00",
    )
    assert repo.today_order_amount(conn, "paper-main", D, market="KR") == Decimal("214500")
    assert repo.today_order_amount(conn, "paper-main", D, market="US") == Decimal(0)
    assert conn.execute("SELECT org_no FROM orders").fetchone()["org_no"] == "91252"


def test_held_since_falls_back_to_accepted_buy_orders(conn):
    dec_id = repo.ingest_checkpoint(
        conn, D, "KR", "005930", "30_decision", {"data": ok_decision(), "call": None}
    )
    repo.insert_order(
        conn,
        decision_id=dec_id,
        account_id="paper-main",
        ticker="005930",
        market="KR",
        side="BUY",
        qty=1,
        order_type="LIMIT",
        price=Decimal("100"),
        status="accepted",
        order_no="1",
        message="",
        mode="auto",
        created_at="2026-10-02T01:00:00+00:00",
    )
    assert repo.held_since(conn, "paper-main") == {"005930": date(2026, 10, 2)}


def test_existing_order_or_approval_lookup(conn):
    dec_id = repo.ingest_checkpoint(
        conn, D, "KR", "005930", "30_decision", {"data": ok_decision(), "call": None}
    )
    assert repo.has_open_order_or_approval(conn, dec_id, "paper-main") is False
    repo.insert_order(
        conn,
        decision_id=dec_id,
        account_id="paper-main",
        ticker="005930",
        market="KR",
        side="BUY",
        qty=1,
        order_type="LIMIT",
        price=Decimal("100"),
        status="failed",
        order_no=None,
        message="x",
        mode="auto",
    )
    assert (
        repo.has_open_order_or_approval(conn, dec_id, "paper-main") is False
    )  # 실패는 재시도 가능
    repo.create_approval(
        conn,
        decision_id=dec_id,
        account_id="paper-main",
        order={},
        requested_at="a",
        expires_at="b",
    )
    assert repo.has_open_order_or_approval(conn, dec_id, "paper-main") is True


def test_latest_nav_before_prefers_latest_row(conn):
    for nav in ("1", "2"):
        repo.snapshot_positions(
            conn,
            account_id="a",
            d=date(2026, 10, 8),
            market="KR",
            nav=Decimal(nav),
            cash=Decimal(nav),
            positions=[],
        )
    assert repo.latest_nav_before(conn, "a", D, market="KR") == Decimal("2")
    assert repo.latest_nav_before(conn, "a", D, market="US") is None


# --- Phase 6: v4 triggers·fills 중복 방지 ------------------------------------------


def test_v4_triggers_and_fill_dedupe(conn):
    from datetime import datetime

    assert db.SCHEMA_VERSION >= 4
    tables = _tables(conn)
    assert "triggers" in tables
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
        qty=2,
        order_type="LIMIT",
        price=Decimal("100"),
        status="accepted",
        order_no="1",
        message="",
        mode="auto",
    )
    kw = dict(
        order_id=oid,
        account_id="paper-main",
        qty=2,
        price=Decimal("100"),
        filled_at="2026-10-09T01:00:00+00:00",
    )
    assert isinstance(repo.insert_fill(conn, **kw), int)
    assert repo.insert_fill(conn, **kw) is None  # 같은 체결은 무시
    assert conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
    tid = repo.insert_trigger(
        conn,
        market="KR",
        ticker="005930",
        kind="move_7pct",
        detail={"pct": 0.08},
        price=Decimal("77000"),
        decision_id=dec_id,
    )
    assert repo.last_trigger_handled_at(conn, "KR", "005930") is None
    repo.mark_trigger_handled(conn, tid, "sess-1")
    assert isinstance(repo.last_trigger_handled_at(conn, "KR", "005930"), datetime)
    row = conn.execute("SELECT kind, detail, session_id FROM triggers").fetchone()
    assert row["kind"] == "move_7pct" and '"pct"' in row["detail"] and row["session_id"] == "sess-1"


def _order(conn, *, side, qty, created_at, order_no, status="accepted", ticker="005930"):
    return repo.insert_order(
        conn,
        decision_id=None,
        account_id="paper-main",
        ticker=ticker,
        market="KR",
        side=side,
        qty=qty,
        order_type="LIMIT",
        price=Decimal("100"),
        status=status,
        order_no=order_no,
        message="",
        mode="auto",
        created_at=created_at,
    )


def test_held_since_resets_after_full_exit_and_reentry(conn):
    b1 = _order(conn, side="BUY", qty=2, created_at="2026-10-01T01:00:00+00:00", order_no="1")
    repo.insert_fill(
        conn,
        order_id=b1,
        account_id="paper-main",
        qty=2,
        price=Decimal("100"),
        filled_at="2026-10-01T01:00:00+00:00",
    )
    s1 = _order(conn, side="SELL", qty=2, created_at="2026-10-05T01:00:00+00:00", order_no="2")
    repo.insert_fill(
        conn,
        order_id=s1,
        account_id="paper-main",
        qty=2,
        price=Decimal("110"),
        filled_at="2026-10-05T01:00:00+00:00",
    )
    assert repo.held_since(conn, "paper-main") == {}
    b2 = _order(conn, side="BUY", qty=3, created_at="2026-10-12T01:00:00+00:00", order_no="3")
    repo.insert_fill(
        conn,
        order_id=b2,
        account_id="paper-main",
        qty=3,
        price=Decimal("100"),
        filled_at="2026-10-12T01:00:00+00:00",
    )
    assert repo.held_since(conn, "paper-main") == {"005930": date(2026, 10, 12)}
    # 일부 매도는 보유 시작일을 바꾸지 않는다
    s2 = _order(conn, side="SELL", qty=1, created_at="2026-10-13T01:00:00+00:00", order_no="4")
    repo.insert_fill(
        conn,
        order_id=s2,
        account_id="paper-main",
        qty=1,
        price=Decimal("100"),
        filled_at="2026-10-13T01:00:00+00:00",
    )
    assert repo.held_since(conn, "paper-main") == {"005930": date(2026, 10, 12)}


def test_held_since_order_fallback_resets_on_accepted_sell(conn):
    _order(conn, side="BUY", qty=1, created_at="2026-10-02T01:00:00+00:00", order_no="1")
    _order(conn, side="SELL", qty=1, created_at="2026-10-06T01:00:00+00:00", order_no="2")
    assert repo.held_since(conn, "paper-main") == {}
    _order(conn, side="BUY", qty=1, created_at="2026-10-07T01:00:00+00:00", order_no="3")
    _order(
        conn,
        side="SELL",
        qty=1,
        created_at="2026-10-08T01:00:00+00:00",
        order_no="4",
        status="rejected",
    )  # 거부된 매도는 보유를 줄이지 않는다
    assert repo.held_since(conn, "paper-main") == {"005930": date(2026, 10, 7)}


def test_has_open_order_today_uses_market_local_day(conn):
    # 23:30 KST 10-08 = 10:30 ET 10-08 에 낸 US 손절 매도
    _order(
        conn,
        side="SELL",
        qty=1,
        created_at="2026-10-08T14:30:00+00:00",
        order_no="1",
        ticker="AAPL",
    )
    conn.execute("UPDATE orders SET market='US'")
    conn.commit()
    # KST 기준 다음 날(00:30 KST 10-09) 점검: KST 창으로는 놓치지만 ET 현지 날짜 창으로는 보인다
    assert repo.has_open_order_today(conn, "paper-main", "AAPL", "SELL", date(2026, 10, 9)) is False
    assert (
        repo.has_open_order_today(
            conn, "paper-main", "AAPL", "SELL", date(2026, 10, 8), market="US"
        )
        is True
    )
    assert (
        repo.has_open_order_today(
            conn, "paper-main", "AAPL", "SELL", date(2026, 10, 9), market="US"
        )
        is False
    )
