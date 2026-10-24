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


def test_snapshot_same_day_removes_sold_positions(conn):
    """같은 날 두 번째 스냅샷에서 사라진 종목(전량 매도)은 지워져야 한다. upsert 만 하면 유령 보유가
    웹·트레이더 입력에 남는다."""
    pos = {
        "ticker": "005930",
        "market": "KR",
        "qty": 10,
        "avg_price": "70000",
        "current_price": "71000",
        "eval_amount": "710000",
    }
    repo.snapshot_positions(
        conn,
        account_id="paper-main",
        d=D,
        market="KR",
        nav=Decimal("1"),
        cash=Decimal("1"),
        positions=[pos],
    )
    repo.snapshot_positions(
        conn,
        account_id="paper-main",
        d=D,
        market="KR",
        nav=Decimal("2"),
        cash=Decimal("2"),
        positions=[],
    )
    snap = views.latest_positions(conn, "paper-main", "KR")
    assert snap["positions"] == [] and snap["nav"] == "2"
    # 다른 시장의 같은 날 행은 건드리지 않는다
    repo.snapshot_positions(
        conn,
        account_id="paper-main",
        d=D,
        market="US",
        nav=Decimal("3"),
        cash=Decimal("3"),
        positions=[{**pos, "ticker": "AAPL", "market": "US"}],
    )
    repo.snapshot_positions(
        conn,
        account_id="paper-main",
        d=D,
        market="KR",
        nav=Decimal("2"),
        cash=Decimal("2"),
        positions=[],
    )
    assert [p["ticker"] for p in views.latest_positions(conn, "paper-main", "US")["positions"]] == [
        "AAPL"
    ]


def test_decision_hydrate_is_shared_between_repo_and_views(conn):
    """결정 행의 json→data 복원은 repo 한 곳(get_decision(hydrate=True)·latest_decision)이고
    views.get_decision 은 그것을 그대로 쓴다."""
    dec_id = _decision(conn)
    a = repo.get_decision(conn, dec_id, hydrate=True)
    b = views.get_decision(conn, dec_id)
    c = repo.latest_decision(conn, "KR", "005930")
    assert a == b == c and a["data"]["action"] == "BUY" and "json" not in a
    assert "json" in repo.get_decision(conn, dec_id)  # 기본은 원시 행


def test_sector_weights_groups_unknown_hedge_and_cash():
    sectors = {"005930": "반도체", "000660": "반도체", "005380": "자동차"}
    rows = views.sector_weights(
        [
            {"ticker": "005930", "eval_amount": "3000000"},
            {"ticker": "000660", "eval_amount": "1500000"},
            {"ticker": "005380", "eval_amount": "1000000"},
            {"ticker": "999999", "eval_amount": "500000"},
            {"ticker": "114800", "eval_amount": "2000000"},
        ],
        "10000000",
        sectors.get,
        hedge_tickers={"114800"},
        cash="2000000",
    )
    assert [r["sector"] for r in rows] == ["반도체", "헤지", "현금", "자동차", "기타"]
    assert (
        rows[0]["weight"] == 0.45
        and rows[0]["count"] == 2
        and rows[0]["tickers"] == ["005930", "000660"]
    )
    assert abs(sum(r["weight"] for r in rows) - 1.0) < 1e-9
    assert views.sector_weights([], None, None) == []
    assert (
        views.sector_weights([{"ticker": "X", "eval_amount": "1"}], "0", None)[0]["weight"] is None
    )
