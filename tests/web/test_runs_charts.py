from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from helpers_agents import ok_decision, ok_turn
from helpers_web import make_deps

from trader.ledger import db, repo
from trader.orchestrator import run_store
from trader.web.app import create_app

D = date(2026, 10, 9)


def _write_run(ticker="005930"):
    d = run_store.run_dir(D, "KR", ticker)
    run_store.write_checkpoint(d, "00_inputs", {"meta": {"ticker": ticker}, "technical": {}})
    run_store.write_checkpoint(
        d, "10_analyst_technical", {"schema_version": 1, "data": {"signal": "BUY"}, "call": None}
    )
    run_store.write_checkpoint(
        d, "20_debate_r0_bull", {"schema_version": 1, "data": ok_turn("bull"), "call": None}
    )
    return d


def test_today_and_checkpoint_read(client):
    _write_run()
    r = client.get("/api/today?market=KR&date=2026-10-09").json()
    assert r["tickers"]["005930"] == ["00_inputs", "10_analyst_technical", "20_debate_r0_bull"]
    assert client.get("/api/today?market=KR").json()["date"] == "2026-10-09"  # now=NOW(KST 15시)
    cp = client.get(
        "/api/checkpoint?ticker=005930&market=KR&date=2026-10-09&stage=10_analyst_technical"
    ).json()
    assert cp["stage"] == "10_analyst_technical" and cp["data"]["signal"] == "BUY"
    assert (
        client.get(
            "/api/checkpoint?ticker=005930&market=KR&date=2026-10-09&stage=30_decision"
        ).status_code
        == 404
    )
    assert client.get("/api/today?market=XX").status_code == 400


def test_checkpoint_path_traversal_rejected(client):
    _write_run()
    bad = [
        "/api/checkpoint?ticker=../005930&market=KR&date=2026-10-09&stage=00_inputs",
        "/api/checkpoint?ticker=005930&market=KR&date=2026-10-09&stage=../../30_decision",
        "/api/checkpoint?ticker=005930&market=KR&date=2026-10-09&stage=00_inputs.json",
        "/api/checkpoint?ticker=005930&market=KR&date=nope&stage=00_inputs",
    ]
    for url in bad:
        assert client.get(url).status_code == 400, url


def test_runs_calls_and_decisions(client, conn):
    rid = db.start_run(conn, "analyze", "KR")
    db.finish_run(conn, rid, "ok")
    conn.execute(
        "INSERT INTO agent_calls(agent, model, cost_usd, status, created_at) "
        "VALUES ('orchestrator', 'sonnet', 1.5, 'ok', ?)",
        ("2026-10-09T05:00:00+00:00",),
    )
    conn.commit()
    dec_id = repo.ingest_checkpoint(conn, D, "KR", "005930", "30_decision", {"data": ok_decision()})
    repo.insert_risk_check(
        conn, decision_id=dec_id, account_id="paper-main", approved=True, adjusted_qty=3, reasons=[]
    )
    assert client.get("/api/runs").json()[0]["status"] == "ok"
    calls = client.get("/api/calls").json()
    assert calls["sessions_today"] == 1 and calls["cost_today"] == 1.5 and len(calls["calls"]) == 1
    decs = client.get("/api/decisions?days=30").json()
    assert decs[0]["ticker"] == "005930" and decs[0]["id"] == dec_id
    out = client.get(f"/api/decisions/{dec_id}/outcomes").json()
    assert out["decision"]["data"]["action"] == "BUY" and out["outcomes"][0]["approved"] is True
    assert client.get("/api/decisions/999/outcomes").status_code == 404


def test_charts_candles_and_markers(client, conn):
    dec_id = repo.ingest_checkpoint(conn, D, "KR", "005930", "30_decision", {"data": ok_decision()})
    repo.insert_order(
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
        order_no="1",
        message="",
        mode="auto",
        created_at="2026-10-09T01:00:00+00:00",
    )
    r = client.get("/api/charts?ticker=005930&market=KR&days=120").json()
    assert len(r["candles"]) == 30 and r["candles"][0]["time"] == "2026-08-20"
    assert isinstance(r["candles"][0]["close"], str)
    kinds = sorted(m["kind"] for m in r["markers"])
    assert kinds == ["decision", "order"]
    order_marker = next(m for m in r["markers"] if m["kind"] == "order")
    assert order_marker["time"] == "2026-10-09" and order_marker["side"] == "BUY"
    assert "paper-main" in order_marker["text"]


def test_charts_account_filter(client, conn):
    dec_id = repo.ingest_checkpoint(conn, D, "KR", "005930", "30_decision", {"data": ok_decision()})
    repo.insert_order(
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
        order_no="1",
        message="",
        mode="auto",
    )
    r = client.get("/api/charts?ticker=005930&market=KR&account_id=real-main").json()
    assert all(m["kind"] != "order" for m in r["markers"])
    assert client.get("/api/charts?ticker=005930&market=KR&account_id=ghost").status_code == 400
    assert client.get("/api/charts?ticker=../x&market=KR").status_code == 400


def test_charts_us_requires_exchange_and_without_market_503(project, conn):
    c = TestClient(create_app(make_deps(project, market=False)))
    assert c.get("/api/charts?ticker=005930&market=KR").status_code == 503
    c2 = TestClient(create_app(make_deps(project)))
    assert c2.get("/api/charts?ticker=AAPL&market=US").status_code == 400
    assert c2.get("/api/charts?ticker=AAPL&market=US&exchange=NAS").status_code == 200


def test_ml_placeholder(client):
    r = client.get("/api/ml/models").json()
    assert r["models"] == [] and "Phase 7" in r["note"]
