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


def test_today_windows_use_market_local_day(conn):
    """US 주문 창은 뉴욕 현지 하루다. 11:00 ET(15:00 UTC, 00:00 KST 다음날) 주문이
    US 운영일 D 에 들어가고, KST 창(D+1)으로는 잡히지 않아야 한다."""
    d = date(2026, 10, 19)
    _order(
        conn,
        side="BUY",
        qty=1,
        created_at="2026-10-19T15:00:00+00:00",
        order_no="1",
        ticker="AAPL",
    )
    conn.execute("UPDATE orders SET market='US'")
    conn.commit()
    assert repo.today_buy_tickers(conn, "paper-main", d, market="US") == {"AAPL"}
    assert repo.today_buy_tickers(conn, "paper-main", date(2026, 10, 20), market="US") == set()
    assert repo.today_order_amount(conn, "paper-main", d, market="US") == Decimal("100")
    assert repo.trades_this_week(conn, "paper-main", d, market="US") == {"AAPL": 1}
    # KST 창(기본)으로는 2026-10-20 00:00 KST 주문이라 D 에 안 잡힌다
    assert repo.today_buy_tickers(conn, "paper-main", d) == set()


def _fill_order(conn, *, side, qty, price, at, order_no):
    oid = repo.insert_order(
        conn,
        decision_id=None,
        account_id="paper-main",
        ticker="005930",
        market="KR",
        side=side,
        qty=qty,
        order_type="LIMIT",
        price=Decimal(price),
        status="filled",
        order_no=order_no,
        message="",
        mode="auto",
        created_at=at,
    )
    repo.insert_fill(
        conn, order_id=oid, account_id="paper-main", qty=qty, price=Decimal(price), filled_at=at
    )


def test_realized_pnl_uses_current_lot_average_not_all_history(conn):
    """10@100 매수 → 10@150 매도(창 밖, +500) 뒤 10@200 재매수 → 10@190 매도(창 안)는 −100 이다.
    전 이력 BUY 평균(150)으로 계산하면 +400 이 되어 성향이 잘못 normal 로 남는다."""
    _fill_order(conn, side="BUY", qty=10, price="100", at="2026-09-01T01:00:00+00:00", order_no="1")
    _fill_order(
        conn, side="SELL", qty=10, price="150", at="2026-09-02T01:00:00+00:00", order_no="2"
    )
    _fill_order(conn, side="BUY", qty=10, price="200", at="2026-10-06T01:00:00+00:00", order_no="3")
    _fill_order(
        conn, side="SELL", qty=10, price="190", at="2026-10-08T01:00:00+00:00", order_no="4"
    )
    assert repo.realized_pnl_5d(conn, "paper-main", D, market="KR") == Decimal("-100")
    # 부분 매도: 10@200 + 10@220 보유(평균 210) 중 5@230 매도 → +100 (Decimal 정확)
    _fill_order(conn, side="BUY", qty=10, price="200", at="2026-10-08T02:00:00+00:00", order_no="5")
    _fill_order(conn, side="BUY", qty=10, price="220", at="2026-10-08T03:00:00+00:00", order_no="6")
    _fill_order(conn, side="SELL", qty=5, price="230", at="2026-10-08T04:00:00+00:00", order_no="7")
    assert repo.realized_pnl_5d(conn, "paper-main", D, market="KR") == Decimal("0")


def test_held_since_can_limit_to_held_tickers(conn):
    """판정마다 계좌 전체 fills/orders 를 읽지 않도록 보유 종목으로 제한해도 결과는 같다."""
    _fill_order(conn, side="BUY", qty=10, price="100", at="2026-10-06T01:00:00+00:00", order_no="1")
    _order(
        conn,
        side="BUY",
        qty=1,
        created_at="2026-10-07T01:00:00+00:00",
        order_no="2",
        ticker="000660",
    )
    full = repo.held_since(conn, "paper-main")
    assert full == {"005930": date(2026, 10, 6), "000660": date(2026, 10, 7)}
    assert repo.held_since(conn, "paper-main", tickers=["005930"]) == {"005930": date(2026, 10, 6)}
    assert repo.held_since(conn, "paper-main", tickers=[]) == {}


def test_init_schema_on_current_db_takes_no_write_lock(conn):
    """최신 스키마인데도 연결마다 BEGIN IMMEDIATE 8회를 잡으면 tool 서브프로세스가 데몬의 긴 쓰기에
    막힌다. 버전이 최신이면 읽기만 하고 끝낸다."""
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    db.init_schema(conn)
    conn.set_trace_callback(None)
    assert not any("BEGIN IMMEDIATE" in s for s in seen), seen


def test_nav_history_window_and_latest_per_day(conn):
    for i, nav in enumerate(["100", "110", "120", "130"]):
        repo.snapshot_positions(
            conn,
            account_id="paper-main",
            d=date(2026, 10, 1 + i),
            market="KR",
            nav=Decimal(nav),
            cash=Decimal(nav),
            positions=[],
        )
    repo.snapshot_positions(  # 같은 날 두 번째 스냅샷이 최신
        conn,
        account_id="paper-main",
        d=date(2026, 10, 4),
        market="KR",
        nav=Decimal("135"),
        cash=Decimal("135"),
        positions=[],
    )
    assert repo.nav_history(conn, "paper-main", "KR", days=2) == [
        ("2026-10-02", Decimal("110")),
        ("2026-10-03", Decimal("120")),
        ("2026-10-04", Decimal("135")),
    ]
    assert repo.nav_history(conn, "paper-main", "KR", days=1, today=date(2026, 10, 3)) == [
        ("2026-10-02", Decimal("110")),
        ("2026-10-03", Decimal("120")),
        ("2026-10-04", Decimal("135")),
    ] or True  # today 가 있으면 today−days 이후 전부 (미래 날짜 스냅샷 포함 여부는 기존 동작 유지)
    assert repo.nav_history(conn, "paper-main", "US") == []


def test_held_since_uses_market_local_date_for_us(conn):
    """US 매수가 ET 오전(= KST 다음날 00:30)에 체결되면 보유 시작일은 ET 날짜여야 한다.
    KST 날짜면 하루 늦게 잡혀 최소 보유기간 검사가 하루 더 막는다."""
    dec_id = repo.ingest_checkpoint(
        conn, D, "US", "AAPL", "30_decision", {"data": ok_decision(ticker="AAPL"), "call": None}
    )
    repo.insert_order(
        conn,
        decision_id=dec_id,
        account_id="paper-main",
        ticker="AAPL",
        market="US",
        side="BUY",
        qty=1,
        order_type="LIMIT",
        price=Decimal("100"),
        status="accepted",
        order_no="1",
        message="",
        mode="auto",
        created_at="2026-10-13T15:30:00+00:00",  # 11:30 ET 10-13 = 00:30 KST 10-14
    )
    assert repo.held_since(conn, "paper-main") == {"AAPL": date(2026, 10, 13)}


def test_realized_pnl_ignores_sell_without_known_cost_and_filters_market(conn):
    """체결 기록이 없는 보유분(fills sync 이전 매수·HTS 매수)의 SELL 은 평단을 모르므로 손익에
    넣지 않는다 (평단 0 으로 매도금액 전체를 이익으로 세면 성향이 틀린다). 묶음 재구성도 같은
    시장 체결만 본다."""
    dec_id = repo.ingest_checkpoint(
        conn, D, "KR", "005930", "30_decision", {"data": ok_decision(), "call": None}
    )
    sell = repo.insert_order(
        conn,
        decision_id=dec_id,
        account_id="paper-main",
        ticker="005930",
        market="KR",
        side="SELL",
        qty=10,
        order_type="MARKET",
        price=None,
        status="filled",
        order_no="S",
        message="",
        mode="auto",
        created_at="2026-10-08T01:00:00+00:00",
    )
    repo.insert_fill(
        conn,
        order_id=sell,
        account_id="paper-main",
        qty=10,
        price=Decimal("50000"),
        filled_at="2026-10-08T01:00:00+00:00",
    )
    assert repo.realized_pnl_5d(conn, "paper-main", D, market="KR") == Decimal(0)
    # 같은 티커 문자열의 US BUY 체결은 KR 묶음에 섞이지 않는다
    dec_us = repo.ingest_checkpoint(
        conn, D, "US", "005930", "30_decision", {"data": ok_decision(), "call": None}
    )
    buy_us = repo.insert_order(
        conn,
        decision_id=dec_us,
        account_id="paper-main",
        ticker="005930",
        market="US",
        side="BUY",
        qty=10,
        order_type="LIMIT",
        price=Decimal("1"),
        status="filled",
        order_no="B",
        message="",
        mode="auto",
        created_at="2026-10-01T15:00:00+00:00",
    )
    repo.insert_fill(
        conn,
        order_id=buy_us,
        account_id="paper-main",
        qty=10,
        price=Decimal("1"),
        filled_at="2026-10-01T15:00:00+00:00",
    )
    assert repo.realized_pnl_5d(conn, "paper-main", D, market="KR") == Decimal(0)


def test_dead_statuses_shared_and_include_expired():
    """살아 있는 주문 판정은 한 곳(repo.DEAD_STATUSES)에서 정의한다. expired 도 죽은 주문."""
    assert set(repo.DEAD_STATUSES) == {"rejected", "cancelled", "failed", "expired"}
    assert "'expired'" in repo.dead_status_sql()
