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


def test_settings_mode_and_keys_include_fo_account(project, conn):
    """설정 화면(계좌 모드·연결 키)은 계좌를 가리지 않는 소비자 — 파생 계좌도 받는다. 현물 계좌
    단위 조회(`require_account`)는 그대로 현물만."""
    from helpers_web import AUTH

    deps = make_deps(project)
    deps.fo_accounts = [fo_account()]
    c = TestClient(create_app(deps), client=("127.0.0.1", 50000), base_url="http://127.0.0.1")
    r = c.post("/api/control/mode", json={"account_id": ACCT, "mode": "off"}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json() == {"account_id": ACCT, "mode": "off"}
    status = c.post("/api/control/secrets/status", headers=AUTH).json()
    keys = {k["name"]: k for k in status["keys"]}
    prefix = "KIS_" + ACCT.upper().replace("-", "_") + "_"
    assert keys[prefix + "APP_KEY"]["account"] == ACCT
    upd = c.post("/api/control/secrets", headers=AUTH, json={"values": {prefix + "HTS_ID": "x"}})
    assert upd.status_code == 200, upd.text
    assert c.get("/api/portfolio", params={"account_id": ACCT}).status_code == 400


def test_accounts_real_fo_mode_shown_off(project, conn):
    deps = make_deps(project)
    deps.fo_accounts = [fo_account(env="real", mode="approve")]
    rows = {r["id"]: r for r in TestClient(create_app(deps)).get("/api/accounts").json()}
    assert rows["real-fo"]["mode"] == "off"


def test_live_ops_and_daemon_jobs_include_fo(project, conn):
    """웹 '지금 점검'·잡 예정표는 데몬 ops_check 와 같은 범위(파생 계좌·파생 잡 포함)."""
    from test_daemon import SCHED

    c, deps = _client(project)
    deps.schedule = SCHED
    ctx = make_ctx(conn)
    _held_position(conn, ctx, qty=1, premium_stop=None)
    conn.execute("UPDATE fo_positions SET status='unknown'")
    conn.commit()
    r = c.get("/api/system/ops", params={"live": "true"}).json()
    assert any(i["key"] == "fo.position_blocked" for i in r["items"]), r["items"]
    jobs = {j["id"] for j in c.get("/api/system/daemon").json()["jobs"]}
    assert {"kr_fo_session", "kr_fo_monitor"} <= jobs


# --- 대시보드 선물 요약·파생 화면 보강 ---------------------------------------------


def _fo_client(project, conn, *, enabled=False, acct=None):
    from trader.fo import store as fo_store

    deps = make_deps(project)
    a = acct or fo_account()
    deps.fo_accounts = [a]
    deps.settings = deps.settings.model_copy(
        update={"fo": deps.settings.fo.model_copy(update={"enabled": enabled})}
    )
    today = deps.now().astimezone(__import__("trader.config", fromlist=["KST"]).KST).date()
    fo_store.write_nav_snapshot(
        conn, account_id=a.id, date=today.isoformat(), nav=Decimal("50000000"),
        cash=Decimal("40000000"), margin_total=Decimal("10000000"), add_margin=Decimal("0"),
    )  # fmt: skip
    conn.commit()
    return TestClient(create_app(deps)), deps, today


def test_fo_summary_states_and_balance(project, conn):
    from trader.control import control
    from trader.fo.executor import CONTRACT_VERSION, verified_key

    c, deps, _ = _fo_client(project, conn)
    row = c.get("/api/fo/summary").json()["accounts"][0]
    assert row["id"] == ACCT and row["state"] == "disabled" and row["mode"] == "auto"
    assert row["nav"] == "50000000" and row["margin_pct"] == 0.2
    assert row["verified"] == {"FUTURES": False, "OPTION_BUY": False, "OPTION_SELL": False}
    c, deps, today = _fo_client(project, conn, enabled=True)
    assert c.get("/api/fo/summary").json()["accounts"][0]["state"] == "unverified"
    control.set_flag(conn, verified_key(deps.settings, deps.fo_accounts[0], "FUTURES"),
                     CONTRACT_VERSION)  # fmt: skip
    conn.commit()
    row = c.get("/api/fo/summary").json()["accounts"][0]
    assert row["state"] == "active" and row["verified"]["FUTURES"] is True
    control.set_flag(conn, f"fo_halted:{ACCT}", today.isoformat())
    conn.commit()
    assert c.get("/api/fo/summary").json()["accounts"][0]["state"] == "halted"
    ctx = make_ctx(conn)
    _held_position(conn, ctx, qty=2, premium_stop=None)
    pos = c.get("/api/fo/summary").json()["accounts"][0]["positions"]
    assert pos == [{"code": "105W11", "kind": "FUTURES", "contract_side": "LONG",
                    "direction": "LONG", "qty": 2, "status": "held"}]  # fmt: skip


def test_fo_summary_real_account_is_off_and_empty_without_fo(project, conn):
    c, _, _ = _fo_client(project, conn, enabled=True, acct=fo_account(env="real", mode="approve"))
    row = c.get("/api/fo/summary").json()["accounts"][0]
    assert row["state"] == "off" and row["mode"] == "off"
    plain = TestClient(create_app(make_deps(project)))
    assert plain.get("/api/fo/summary").json() == {"accounts": []}


def test_fo_account_context_and_triggers(project, conn):
    c, deps, today = _fo_client(project, conn)
    conn.execute(
        "INSERT INTO triggers(account_id, market, ticker, kind, detail, price, created_at) "
        "VALUES (?, 'KR', '105W11', 'fo:move', '{}', '350', ?)",
        (ACCT, deps.now().isoformat()),
    )
    conn.commit()
    r = c.get("/api/fo/account", params={"account_id": ACCT}).json()
    assert r["status"]["id"] == ACCT and r["status"]["state"] == "disabled"
    lim = r["context"]["limits"]
    assert lim["max_leverage"] == deps.settings.fo.max_leverage and lim["enabled"] is False
    assert r["context"]["source"] == "settings"  # 오늘 _fo_inputs 가 없으면 설정값
    assert [t["kind"] for t in r["triggers"]] == ["move"]
    assert c.get("/api/fo/account", params={"account_id": "paper-main"}).status_code == 400


def test_fo_today_checks_include_gate_detail(project, conn):
    from trader.orchestrator import run_store

    c, deps, today = _fo_client(project, conn)
    mdir = run_store.market_dir(today, "KR", deps.state, create=True)
    verdict = {"approved": True, "reasons": [], "note": "", "leverage_used": 0.4,
               "exposure_after": "20000000", "stress_loss_after": "900000",
               "lifetime_budget": {"used": "0", "planned": "300000", "limit": "1000000",
                                   "free": "700000"},
               "cross_factor": 1.0, "n_target": 2, "approved_mult": None}  # fmt: skip
    payload = run_store.checkpoint_payload({"verdict": verdict}, None)
    run_store.write_checkpoint(mdir, f"_fo_risk_check_{ACCT}", payload)
    chk = c.get("/api/fo/today").json()["checks"][f"_fo_risk_check_{ACCT}"]
    assert chk["leverage_used"] == 0.4 and chk["n_target"] == 2
    assert chk["lifetime_budget"]["limit"] == "1000000" and chk["stress_loss_after"] == "900000"
