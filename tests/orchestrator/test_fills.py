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
        created_at="2026-10-09T00:10:00+00:00",  # 09:10 KST, 체결(10:05 KST)과 같은 현지 날짜
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


def test_sync_cumulative_fill_updates_not_duplicates(conn):
    """KIS 체결 조회는 주문당 누적 수량이라 장중 2주 → 마감 5주는 한 행이 5주로 갱신돼야 한다."""
    acct = make_account("paper-main", "paper", "auto")
    oid = _order(conn, "A", qty=5)
    broker = FakeBroker(acct, fills={("KR", D): [_fill("A", qty=2)]})
    fills_mod.sync_fills(conn, broker, acct, D)
    broker.fills[("KR", D)] = [_fill("A", qty=5, price="70950")]
    r = fills_mod.sync_fills(conn, broker, acct, D)
    rows = conn.execute("SELECT qty, price FROM fills WHERE order_id=?", (oid,)).fetchall()
    assert len(rows) == 1 and rows[0]["qty"] == 5 and rows[0]["price"] == "70950"
    assert r["updated"] == 1
    assert (
        conn.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()["status"]
        == "filled"
    )


def test_sync_fills_datetime_uses_market_local_date(conn):
    """07:10 KST(=전날 18:10 ET) 동기화: US 는 ET 날짜(전날), KR 은 KST 날짜로 조회해야 한다."""
    acct = make_account("paper-main", "paper", "auto", ["KR", "US"])
    us = Fill(
        order_no="U1",
        ticker="AAPL",
        market="US",
        side="BUY",
        qty=1,
        price=Decimal("200"),
        filled_at=datetime(2026, 10, 8, 14, 0, tzinfo=UTC),
    )
    broker = FakeBroker(
        acct,
        fills={("KR", date(2026, 10, 9)): [_fill("K1")], ("US", date(2026, 10, 8)): [us]},
    )
    now = datetime(2026, 10, 8, 22, 10, tzinfo=UTC)  # 2026-10-09 07:10 KST, 2026-10-08 18:10 ET
    r = fills_mod.sync_fills(conn, broker, acct, now)
    assert r["inserted"] == 2
    assert r["date"] == {"KR": "2026-10-09", "US": "2026-10-08"}


def test_order_no_match_requires_same_ticker_and_side(conn):
    """KIS 주문번호는 날마다 다시 시작하므로 종목·방향이 다른 옛 주문에 체결을 붙이지 않는다."""
    acct = make_account("paper-main", "paper", "auto")
    old = _order(conn, "0000000012", qty=3)  # 005930 BUY
    broker = FakeBroker(
        acct, fills={("KR", D): [_fill("0000000012", ticker="000660", side="SELL", qty=2)]}
    )
    fills_mod.sync_fills(conn, broker, acct, D)
    # 체결은 붙지 않았다 (옛 날짜를 마감 뒤 동기화했으니 미체결 BUY 는 expired 로 끝난다)
    assert conn.execute("SELECT status FROM orders WHERE id=?", (old,)).fetchone()["status"] == (
        "expired"
    )
    assert conn.execute("SELECT COUNT(*) FROM fills WHERE order_id=?", (old,)).fetchone()[0] == 0
    ext = conn.execute("SELECT * FROM orders WHERE ticker='000660'").fetchone()
    assert ext["mode"] == "external" and ext["side"] == "SELL" and ext["status"] == "filled"


def test_fill_does_not_attach_to_old_order_with_reused_order_no(conn):
    """KIS 주문번호는 날마다 다시 시작한다. 8일 뒤 같은 번호·종목·방향의 외부 체결이 옛 원장 주문에
    붙어 기존 체결을 지우면 안 된다 → 같은 현지 날짜의 주문만 매칭하고, 없으면 external 행."""
    acct = make_account("paper-main", "paper", "auto")
    old = _order(conn, "0000000012", qty=3)
    fills_mod.sync_fills(conn, FakeBroker(acct, fills={("KR", D): [_fill("0000000012")]}), acct, D)
    assert conn.execute("SELECT COUNT(*) AS n FROM fills WHERE order_id=?", (old,)).fetchone()[
        "n"
    ] == 1
    later = date(2026, 10, 17)
    f2 = Fill(
        order_no="0000000012",
        ticker="005930",
        market="KR",
        side="BUY",
        qty=5,
        price=Decimal("72000"),
        filled_at=datetime(2026, 10, 17, 2, 0, tzinfo=UTC),
    )
    r = fills_mod.sync_fills(conn, FakeBroker(acct, fills={("KR", later): [f2]}), acct, later)
    assert r["inserted"] == 1 and r["updated"] == 0
    rows = conn.execute("SELECT id, mode, status FROM orders ORDER BY id").fetchall()
    assert len(rows) == 2 and rows[1]["mode"] == "external" and rows[1]["status"] == "filled"
    # 옛 주문의 체결은 그대로
    q = conn.execute("SELECT SUM(qty) AS q FROM fills WHERE order_id=?", (old,)).fetchone()["q"]
    assert q == 3


def test_fill_attaches_to_unknown_order_of_same_day(conn):
    """응답을 못 받아 unknown 으로 남은 주문(주문번호 없음)에 같은 날·종목·방향 체결이 오면
    external 행을 만들지 않고 그 주문에 붙여 accepted→filled 로 확정한다."""
    acct = make_account("paper-main", "paper", "auto")
    dec_id = repo.ingest_checkpoint(conn, D, "KR", "005930", "30_decision", {"data": ok_decision()})
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
        status="unknown",
        order_no=None,
        message="NETWORK_UNKNOWN",
        mode="auto",
        created_at="2026-10-09T00:10:00+00:00",
    )
    broker = FakeBroker(acct, fills={("KR", D): [_fill("0000117057")]})
    fills_mod.sync_fills(conn, broker, acct, D)
    rows = conn.execute("SELECT id, status, order_no, mode FROM orders ORDER BY id").fetchall()
    assert len(rows) == 1 and rows[0]["id"] == oid
    row = rows[0]
    assert (row["status"], row["order_no"], row["mode"]) == ("filled", "0000117057", "auto")


def _order_with(conn, *, status, order_no, created_at, market="KR", ticker="005930", side="BUY"):
    dec_id = repo.ingest_checkpoint(conn, D, market, ticker, "30_decision", {"data": ok_decision()})
    return repo.insert_order(
        conn,
        decision_id=dec_id,
        account_id="paper-main",
        ticker=ticker,
        market=market,
        side=side,
        qty=3,
        order_type="LIMIT",
        price=Decimal("71000"),
        status=status,
        order_no=order_no,
        message="",
        mode="auto",
        created_at=created_at,
    )


def test_sync_after_close_expires_unfilled_orders_of_that_day(conn):
    """장 마감 뒤 체결 조회를 마쳤는데 그날 accepted·unknown 주문에 체결이 없으면 expired 로 바꾼다
    (당일 주문은 마감에 소멸). 체결이 붙은 주문·다른 시장·다른 날 주문은 건드리지 않는다."""
    acct = make_account("paper-main", "paper", "auto", ["KR", "US"])
    t0 = "2026-10-09T00:10:00+00:00"
    o_acc = _order_with(conn, status="accepted", order_no="A", created_at=t0)
    o_unk = _order_with(conn, status="unknown", order_no=None, created_at=t0)
    t1, t_old, t_us = (
        "2026-10-09T00:30:00+00:00",
        "2026-10-08T00:30:00+00:00",
        "2026-10-09T14:00:00+00:00",
    )
    o_fill = _order_with(conn, status="accepted", order_no="F", created_at=t1, ticker="000660")
    o_old = _order_with(conn, status="accepted", order_no="O", created_at=t_old)
    o_us = _order_with(
        conn, status="accepted", order_no="U", created_at=t_us, market="US", ticker="AAPL"
    )
    broker = FakeBroker(acct, fills={("KR", D): [_fill("F", ticker="000660")]})
    after_close = datetime(2026, 10, 9, 7, 0, tzinfo=UTC)  # 16:00 KST
    r = fills_mod.sync_fills(conn, broker, acct, D, now=lambda: after_close)
    st = {row["id"]: row["status"] for row in conn.execute("SELECT id, status FROM orders")}
    assert st[o_acc] == "expired" and st[o_unk] == "expired"
    assert st[o_fill] == "filled" and st[o_old] == "accepted" and st[o_us] == "accepted"
    assert r["expired"] == 2
    # 만료된 주문은 살아 있는 주문이 아니다
    assert not repo.has_open_order_today(conn, "paper-main", "005930", "BUY", D, market="KR")
    assert "005930" not in repo.today_buy_tickers(conn, "paper-main", D, market="KR")


def test_sync_during_session_does_not_expire(conn):
    """장중(마감 전) 수동 fills sync 는 미체결 주문을 만료시키지 않는다."""
    acct = make_account("paper-main", "paper", "auto", ["KR"])
    oid = _order_with(conn, status="accepted", order_no="A", created_at="2026-10-09T00:10:00+00:00")
    broker = FakeBroker(acct, fills={("KR", D): []})
    r = fills_mod.sync_fills(
        conn, broker, acct, D, now=lambda: datetime(2026, 10, 9, 3, 0, tzinfo=UTC)  # 12:00 KST
    )
    status = conn.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()["status"]
    assert status == "accepted" and r["expired"] == 0


def test_sync_fetch_failure_does_not_expire(conn):
    acct = make_account("paper-main", "paper", "auto", ["KR"])
    oid = _order_with(conn, status="accepted", order_no="A", created_at="2026-10-09T00:10:00+00:00")
    broker = FakeBroker(acct, fail={"get_fills"})
    after_close = datetime(2026, 10, 9, 7, 0, tzinfo=UTC)
    fills_mod.sync_fills(conn, broker, acct, D, now=lambda: after_close)
    status = conn.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()["status"]
    assert status == "accepted"
