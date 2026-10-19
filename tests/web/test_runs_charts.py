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


def test_today_hides_empty_dirs_and_read_does_not_create(client):
    _write_run()
    (run_store.run_dir(D, "KR", "000660").parent / "_").mkdir(exist_ok=True)
    assert "_" not in client.get("/api/today?market=KR&date=2026-10-09").json()["tickers"]
    r = client.get("/api/checkpoint?ticker=999999&market=KR&date=2026-10-09&stage=00_inputs")
    assert r.status_code == 404
    assert not (run_store.run_dir(D, "KR", "000660").parent / "999999").exists()


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
    assert r["models"] == [] and r["active"] is None and "승격" in r["note"]


def test_charts_days_bounds_and_cache(client, deps):
    assert client.get("/api/charts?ticker=005930&market=KR&days=0").status_code == 422
    assert client.get("/api/charts?ticker=005930&market=KR&days=5000").status_code == 422
    before = len(deps.market.calls)
    assert client.get("/api/charts?ticker=005930&market=KR&days=60").status_code == 200
    assert client.get("/api/charts?ticker=005930&market=KR&days=60").status_code == 200
    assert len(deps.market.calls) == before + 1  # 같은 날·같은 키는 캐시


def test_query_bounds_on_limits_and_days(client):
    assert client.get("/api/orders?account_id=paper-main&limit=-1").status_code == 422
    assert client.get("/api/orders?account_id=paper-main&limit=10000").status_code == 422
    assert client.get("/api/decisions?days=1000000000").status_code == 422
    assert client.get("/api/calls?days=0").status_code == 422
    assert client.get("/api/runs?limit=0").status_code == 422


# --- 분봉 차트 -----------------------------------------------------------------


def _order(conn, dec_id, created_at, account_id="paper-main"):
    repo.insert_order(
        conn,
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
        created_at=created_at,
    )


def test_minute_chart_epoch_and_markers_same_day_only(client, conn):
    from datetime import UTC, datetime

    dec_id = repo.ingest_checkpoint(conn, D, "KR", "005930", "30_decision", {"data": ok_decision()})
    _order(conn, dec_id, "2026-10-09T00:02:30+00:00")  # 09:02 KST 오늘
    _order(conn, dec_id, "2026-10-08T00:02:30+00:00")  # 어제 → 제외
    conn.execute(
        "INSERT INTO triggers(account_id, market, ticker, kind, detail, created_at) "
        "VALUES (?,?,?,?,?,?)",
        ("paper-main", "KR", "005930", "take_profit", "+7%", "2026-10-09T00:03:10+00:00"),
    )
    conn.commit()
    r = client.get("/api/charts/minute?ticker=005930&market=KR&count=5").json()
    assert r["interval"] == "1m" and r["tz"] == "Asia/Seoul" and r["session_date"] == "2026-10-09"
    t0 = int(datetime(2026, 10, 9, 0, 0, tzinfo=UTC).timestamp())
    assert [c["time"] for c in r["candles"]] == [t0 + 60 * i for i in range(5)]
    assert isinstance(r["candles"][0]["close"], str)
    assert [(m["kind"], m["time"]) for m in r["markers"]] == [
        ("order", t0 + 120),
        ("trigger", t0 + 180),
    ]
    assert "트리거 take_profit" in r["markers"][1]["text"]
    assert r["markers"][0]["text"].startswith("09:02 paper-main BUY")  # 현지 시각 접두사
    assert r["markers"][1]["text"].startswith("09:03 트리거")
    assert all(m["kind"] != "decision" for m in r["markers"])


def test_minute_chart_account_filter_orders_only(client, conn):
    dec_id = repo.ingest_checkpoint(conn, D, "KR", "005930", "30_decision", {"data": ok_decision()})
    _order(conn, dec_id, "2026-10-09T00:02:30+00:00")
    r = client.get("/api/charts/minute?ticker=005930&market=KR&account_id=real-main").json()
    assert all(m["kind"] != "order" for m in r["markers"])


def test_minute_chart_us_epoch_uses_new_york(client):
    from datetime import UTC, datetime

    r = client.get("/api/charts/minute?ticker=AAPL&market=US&exchange=NAS&count=2").json()
    # 가짜 분봉 5개(09:00~09:04) 중 최신 2개 → 첫 봉 09:03 뉴욕(EDT, UTC−4) = 13:03Z
    assert r["tz"] == "America/New_York"
    assert r["candles"][0]["time"] == int(datetime(2026, 10, 9, 13, 3, tzinfo=UTC).timestamp())


def test_minute_chart_empty_candles_ok(client):
    r = client.get("/api/charts/minute?ticker=000660&market=KR")
    assert r.status_code == 200 and r.json()["candles"] == [] and r.json()["markers"] == []
    assert r.json()["session_date"] is None


def test_minute_chart_bounds(client, project):
    assert client.get("/api/charts/minute?ticker=005930&market=KR&count=0").status_code == 422
    assert client.get("/api/charts/minute?ticker=005930&market=KR&count=391").status_code == 422
    assert (
        client.get("/api/charts/minute?ticker=005930&market=KR&account_id=ghost").status_code == 400
    )
    assert client.get("/api/charts/minute?ticker=AAPL&market=US").status_code == 400
    assert client.get("/api/charts/minute?ticker=../x&market=KR").status_code == 400
    c = TestClient(create_app(make_deps(project, market=False)))
    assert c.get("/api/charts/minute?ticker=005930&market=KR").status_code == 503


def test_minute_chart_cache_and_count_passthrough(client, deps):
    before = len(deps.market.calls)
    assert client.get("/api/charts/minute?ticker=005930&market=KR&count=3").status_code == 200
    assert client.get("/api/charts/minute?ticker=005930&market=KR&count=3").status_code == 200
    assert deps.market.calls[before:] == ["get_minute_kr"]  # 60초 캐시


def test_chart_cache_prunes_expired_entries():
    from trader.web.routers import charts as charts_mod

    cache: dict = {("KR", "a"): (0.0, []), ("KR", "b"): (10_000.0, [])}
    charts_mod._remember(cache, ("KR", "c"), [], ttl=1.0, now=10_000.5)
    assert set(cache) == {("KR", "b"), ("KR", "c")}


def test_us_daily_order_marker_uses_market_local_date(client, conn):
    """US 주문 마커는 뉴욕 날짜 봉에 붙어야 한다 (KST 날짜면 자정 뒤 주문이 다음 날·주말 봉으로)."""
    dec_id = repo.ingest_checkpoint(
        conn, D, "US", "AAPL", "30_decision", {"data": ok_decision(ticker="AAPL")}
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
        price=Decimal("200"),
        status="accepted",
        order_no="1",
        message="",
        mode="auto",
        created_at="2026-10-09T15:30:00+00:00",  # 11:30 ET 10-09 = 00:30 KST 10-10
    )
    r = client.get("/api/charts?ticker=AAPL&market=US&exchange=NAS&days=120").json()
    order_marker = next(m for m in r["markers"] if m["kind"] == "order")
    assert order_marker["time"] == "2026-10-09"
