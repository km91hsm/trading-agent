from datetime import date
from decimal import Decimal

from helpers_agents import ok_decision

from trader.ledger import repo, views

D = date(2026, 10, 9)


def _decision(conn, ticker="005930"):
    return repo.ingest_checkpoint(
        conn, D, "KR", ticker, "30_decision", {"data": ok_decision(ticker=ticker)}
    )


def _order(conn, dec_id, account_id, **over):
    kw = dict(
        decision_id=dec_id,
        account_id=account_id,
        ticker="005930",
        market="KR",
        side="BUY",
        qty=1,
        order_type="LIMIT",
        price=Decimal("71000"),
        status="accepted",
        order_no="1",
        message="",
        mode="auto",
    )
    kw.update(over)
    return repo.insert_order(conn, **kw)


def test_list_orders_filters_account(conn):
    dec_id = _decision(conn)
    _order(conn, dec_id, "paper-main")
    _order(conn, dec_id, "paper-main", order_no="2")
    _order(conn, dec_id, "real-main", order_no="3", mode="off")
    rows = views.list_orders(conn, "paper-main")
    assert [r["order_no"] for r in rows] == ["2", "1"] and rows[0]["price"] == "71000"
    assert views.list_orders(conn, "real-main")[0]["order_no"] == "3"
    assert views.list_orders(conn, "ghost") == []


def test_latest_positions_excludes_cash_rows_and_picks_latest_date(conn):
    repo.snapshot_positions(
        conn,
        account_id="paper-main",
        d=date(2026, 10, 8),
        market="KR",
        nav=Decimal("1"),
        cash=Decimal("1"),
        positions=[],
    )
    repo.snapshot_positions(
        conn,
        account_id="paper-main",
        d=D,
        market="KR",
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
    snap = views.latest_positions(conn, "paper-main", "KR")
    assert snap["date"] == "2026-10-09" and snap["nav"] == "1000000"
    assert [p["ticker"] for p in snap["positions"]] == ["005930"]
    assert views.latest_positions(conn, "paper-main", "US") is None
    assert views.latest_positions(conn, "real-main", "KR") is None


def test_decision_outcomes_joins_checks_and_orders(conn):
    dec_id = _decision(conn)
    repo.insert_risk_check(
        conn,
        decision_id=dec_id,
        account_id="paper-main",
        approved=True,
        adjusted_qty=13,
        reasons=[],
    )
    repo.insert_risk_check(
        conn,
        decision_id=dec_id,
        account_id="real-main",
        approved=False,
        adjusted_qty=0,
        reasons=["HOLD: 주문 없음"],
    )
    _order(conn, dec_id, "paper-main", qty=13)
    out = {o["account_id"]: o for o in views.decision_outcomes(conn, dec_id)}
    assert out["paper-main"]["approved"] is True and out["paper-main"]["order_status"] == "accepted"
    assert out["real-main"]["approved"] is False and out["real-main"]["order_status"] is None
    assert out["real-main"]["reasons"] == ["HOLD: 주문 없음"]
    decs = views.list_decisions(conn, days=30)
    assert len(decs) == 1 and decs[0]["ticker"] == "005930" and "json" not in decs[0]


def test_account_summary_counts_pending(conn):
    dec_id = _decision(conn)
    repo.create_approval(
        conn,
        decision_id=dec_id,
        account_id="paper-main",
        order={},
        requested_at="a",
        expires_at="b",
    )
    repo.snapshot_positions(
        conn,
        account_id="paper-main",
        d=D,
        market="KR",
        nav=Decimal("10000000"),
        cash=Decimal("10000000"),
        positions=[],
    )
    s = views.account_summary(conn, "paper-main", D)
    assert s["pending_approvals"] == 1 and s["nav_by_market"] == {"KR": "10000000", "US": None}
    assert s["stance_by_market"] == {"KR": "normal", "US": "normal"} and "stance" not in s
    assert views.list_approvals(conn, "paper-main", status="pending")[0]["order"] == {}
    assert views.list_approvals(conn, "real-main") == []
