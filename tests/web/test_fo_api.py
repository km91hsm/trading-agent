"""Phase 26 Task 4.5: 파생 웹 API·계좌 asset 꼬리표·전체 합산 제외·SSE 지문."""

from __future__ import annotations

from contextlib import closing
from decimal import Decimal

from fastapi.testclient import TestClient
from fo.test_executor import ACCT, _held_position, decision, env, make_ctx
from fo.test_executor import account as fo_account
from helpers_web import make_deps

from trader.fo import executor, store
from trader.ledger import db
from trader.web import sse
from trader.web.app import create_app


def _client(project):
    deps = make_deps(project)
    deps.fo_accounts = [fo_account()]
    return TestClient(create_app(deps)), deps


def test_accounts_lists_fo_with_asset_tag(project, conn):
    c, _ = _client(project)
    rows = c.get("/api/accounts").json()
    by = {r["id"]: r for r in rows}
    assert by[ACCT]["asset"] == "fo"
    assert all(r["asset"] == "stock" for r in rows if r["id"] != ACCT)


def test_fo_positions_requires_account_id(project, conn):
    c, _ = _client(project)
    assert c.get("/api/fo/positions").status_code == 422
    assert c.get("/api/fo/positions", params={"account_id": "paper-main"}).status_code == 400
    ctx = make_ctx(conn)
    _held_position(conn, ctx, qty=2, premium_stop=None)
    rows = c.get("/api/fo/positions", params={"account_id": ACCT}).json()
    assert rows[0]["code"] == "105W11" and rows[0]["qty_held"] == 2
    assert rows[0]["direction"] == "LONG" and rows[0]["budget_limit"] is not None


def test_fo_today_and_legs_shapes(project, conn):
    c, deps = _client(project)
    ctx = make_ctx(conn)  # 운영일 2026-10-27 10:30 KST
    ctx.state = deps.state
    out = executor.run_leg(ctx, executor.account_of(ctx, ACCT), env(decision()))
    assert out[0]["status"] == "accepted"
    store.record_decision(conn, date="2026-10-27",
                          account_id=None, position_id=None, round=0, request_id="r1",
                          intent="OPEN", direction="LONG", vehicle="FUTURES",
                          target_leverage=1.0, confidence=0.7, index_at_decision="350",
                          quote_asof=None, data='{"decision": {"stop_pt": 345.0}}')  # fmt: skip
    conn.commit()
    today = c.get("/api/fo/today", params={"date": "2026-10-27"}).json()
    assert today["decision"]["intent"] == "OPEN" and today["decision"]["stop_pt"] == 345.0
    assert today["accounts"][0]["id"] == ACCT
    assert f"_fo_risk_check_{ACCT}" in today["checks"]
    legs = c.get("/api/fo/legs", params={"account_id": ACCT, "date": "2026-10-27"}).json()
    assert legs and legs[0]["intent"] == "OPEN" and legs[0]["status"] == "accepted"


def test_fo_scorecard_and_report(project, conn):
    c, _ = _client(project)
    assert c.get("/api/fo/scorecard").json()["matured"] == 0
    store.write_nav_snapshot(conn, account_id=ACCT, date="2026-10-26", nav=Decimal("100"),
                             cash=Decimal("50"), margin_total=Decimal(0),
                             add_margin=Decimal(0))  # fmt: skip
    store.write_nav_snapshot(conn, account_id=ACCT, date="2026-10-27", nav=Decimal("110"),
                             cash=Decimal("50"), margin_total=Decimal(0),
                             add_margin=Decimal(0))  # fmt: skip
    conn.commit()
    rep = c.get("/api/fo/report", params={"account_id": ACCT, "days": 30}).json()
    assert [x["nav"] for x in rep["nav"]][-1] == "110" and abs(rep["return"] - 0.1) < 1e-9


def test_overview_excludes_fo(project, conn):
    c, deps = _client(project)
    deps.accounts.append(fo_account())  # 잘못 섞여 들어와도 합산은 현물만
    store.write_nav_snapshot(conn, account_id=ACCT, date="2026-10-27", nav=Decimal("999999"),
                             cash=Decimal(0), margin_total=Decimal(0),
                             add_margin=Decimal(0))  # fmt: skip
    conn.commit()
    body = c.get("/api/portfolio/overview").json()
    assert ACCT not in str(body)


def test_sse_fingerprint_changes_on_leg_update(project, conn):
    ctx = make_ctx(conn)
    pos = _held_position(conn, ctx, qty=2)
    with closing(db.connect()) as c2:
        before = sse.snapshot(c2)["fo"]
    with store.tx(conn):
        leg_id = store.insert_leg(conn, position_id=pos.id, account_id=ACCT, intent="CLOSE",
                                  round=0, attempt=1, qty=2, price=None, order_type="MARKET")
    with closing(db.connect()) as c2:
        mid = sse.snapshot(c2)["fo"]
    with store.tx(conn):
        store.set_leg(conn, leg_id, status="accepted", order_no="X1")
    with closing(db.connect()) as c2:
        after = sse.snapshot(c2)["fo"]
    assert before != mid != after
