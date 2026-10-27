"""Phase 26 단계 3 Task 3.4: `tool fo bundle|write|summarize`·`submit-fo-decision` — 쓰기 허용 목록·
파일명↔페이로드·요약 무효화·제출 전제·봉투 대조·버전·계약 해석·노후 입력·멱등·진입 창 밖 보류."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from fo.test_executor import settings
from fo.test_inputs import _ctx as fo_ctx
from helpers_agents import ok_report, ok_turn
from helpers_sources import T0

from trader.broker.fo import CONTRACT_VERSION
from trader.control import control
from trader.fo import executor, store
from trader.fo import inputs as inputs_mod
from trader.orchestrator import tools
from trader.orchestrator.run_store import list_checkpoints, market_dir, read_checkpoint

D = T0.date()
ACCT = "paper-fo"


def _fo(conn, *, now=T0, asof=None, enabled=True):
    fo = fo_ctx(conn, now=now)
    fo.settings = settings(enabled=enabled)
    for v in ("FUTURES", "OPTION_BUY", "OPTION_SELL"):  # 실기 검증 플래그(없으면 신규 거부)
        key = executor.verified_key(fo.settings, fo.accounts[0], v)
        control.set_flag(conn, key, CONTRACT_VERSION)
    inp = inputs_mod.build_inputs(fo, today=D, asof=asof or now, accounts=fo.accounts)
    inputs_mod.save_inputs(inp, today=D, state=None)
    return fo, inp


@pytest.fixture
def ctx(project, conn):
    fo, inp = _fo(conn)
    c = tools.ToolContext(
        settings=fo.settings, accounts=[], conn=conn, now=lambda: T0, fo=fo, dry_run=False
    )
    c._inputs = inp  # type: ignore[attr-defined]
    return c


def _reports(ctx, roles=("technical", "sentiment")):
    for r in roles:
        out = tools.fo_write(
            ctx, date=D, stage=f"_fo_analyst_{r}", payload=ok_report(ticker="KOSPI200", role=r),
            agent=f"analyst-{r}",
        )  # fmt: skip
        assert out["ok"], out


def _debate(ctx, rounds=(0,)):
    for n in rounds:
        for side in ("bull", "bear"):
            out = tools.fo_write(
                ctx, date=D, stage=f"_fo_debate_r{n}_{side}", payload=ok_turn(side, n),
                agent=f"researcher-{side}",
            )  # fmt: skip
            assert out["ok"], out


def _ready(ctx):
    _reports(ctx)
    _debate(ctx)
    assert tools.fo_summarize(ctx, date=D)["ok"]


def _env(ctx, **dec):
    base = dict(
        intent="OPEN", direction="LONG", vehicle="FUTURES", target_leverage=1.0,
        order_type="LIMIT", entry_price=350.5, stop_pt=345.0, take_profit_pt=360.0,
        confidence=0.7, rationale="t", horizon_days=5,
    )  # fmt: skip
    base.update(dec)
    return {"decision": base, "request_id": ctx._inputs["meta"]["request_id"]}


def test_fo_write_allowlist_and_name_mismatch(ctx):
    rep = ok_report(ticker="KOSPI200", role="technical")
    assert tools.fo_write(ctx, date=D, stage="_fo_decision", payload={})["ok"] is False
    assert tools.fo_write(ctx, date=D, stage="_fo_summary", payload={})["ok"] is False
    assert tools.fo_write(ctx, date=D, stage="_fo_analyst_fundamental", payload=rep)["ok"] is False
    bad = tools.fo_write(ctx, date=D, stage="_fo_analyst_sentiment", payload=rep)
    assert bad["ok"] is False and "role" in bad["error"]
    ok = tools.fo_write(
        ctx, date=D, stage="_fo_analyst_technical", payload=rep, agent="analyst-technical"
    )
    saved = read_checkpoint(market_dir(D, "KR"), "_fo_analyst_technical")
    assert ok["ok"] and saved["call"]["agent"] == (
        "analyst-technical"
    )
    mm = tools.fo_write(ctx, date=D, stage="_fo_debate_r0_bull", payload=ok_turn("bear", 0))
    assert mm["ok"] is False and "불일치" in mm["error"]
    far = tools.fo_write(ctx, date=D, stage="_fo_debate_r7_bull", payload=ok_turn("bull", 7))
    assert far["ok"] is False and "debate_rounds" in far["error"]
    # HTML 엔티티 언이스케이프
    esc = ok_report(ticker="KOSPI200", role="sentiment", summary="외인 &gt; 기관")
    w = tools.fo_write(ctx, date=D, stage="_fo_analyst_sentiment", payload=esc)
    assert w["ok"]
    saved = read_checkpoint(market_dir(D, "KR"), "_fo_analyst_sentiment")
    assert saved["data"]["summary"] == "외인 > 기관"


def test_fo_write_requires_inputs(project, conn):
    fo = fo_ctx(conn)
    c = tools.ToolContext(settings=settings(), accounts=[], conn=conn, now=lambda: T0, fo=fo)
    r = tools.fo_write(
        c, date=D, stage="_fo_analyst_technical", payload=ok_report(role="technical")
    )
    assert r["ok"] is False and "_fo_inputs" in r["error"]
    assert tools.fo_bundle(c, date=D, kind="analyst", role="technical")["ok"] is False


def test_fo_write_invalidates_summary(ctx):
    _ready(ctx)
    assert "_fo_summary" in list_checkpoints(market_dir(D, "KR"))
    _debate(ctx, rounds=(1,))
    assert "_fo_summary" not in list_checkpoints(market_dir(D, "KR"))
    s = tools.fo_summarize(ctx, date=D)
    assert s["ok"] and s["rounds"] == 1


def test_fo_bundle_shapes(ctx):
    a = tools.fo_bundle(ctx, date=D, kind="analyst", role="technical")
    assert a["ok"] and a["data"]["role"] == "technical" and "indicators" in a["data"]["technical"]
    m = tools.fo_bundle(ctx, date=D, kind="analyst", role="news_macro")
    assert m["ok"] and m["data"]["proxy"] == "KOSPI200" and "regime" in m["data"]
    assert tools.fo_bundle(ctx, date=D, kind="researcher", round=0, side="bull")["ok"] is False
    _reports(ctx)
    r0 = tools.fo_bundle(ctx, date=D, kind="researcher", round=0, side="bull")
    assert r0["ok"] and len(r0["data"]["reports"]) == 2 and r0["data"]["own_previous"] is None
    assert tools.fo_bundle(ctx, date=D, kind="researcher", round=1, side="bull")["ok"] is False
    _debate(ctx)
    r1 = tools.fo_bundle(ctx, date=D, kind="researcher", round=1, side="bear")
    assert r1["ok"] and r1["data"]["opponent_previous"]["side"] == "bull"
    assert tools.fo_bundle(ctx, date=D, kind="trader")["ok"] is False  # 요약 없음
    tools.fo_summarize(ctx, date=D)
    t = tools.fo_bundle(ctx, date=D, kind="trader")
    assert t["ok"] and t["data"]["request_id"] == ctx._inputs["meta"]["request_id"]
    assert t["data"]["context"]["position"] is None and t["data"]["previous_decision"] is None
    assert t["data"]["context"]["limits"]["max_leverage"] == 2.0


def test_submit_requires_two_roles_debate_summary(ctx):
    r = tools.submit_fo_decision(ctx, date=D, payload=_env(ctx))
    assert r["ok"] is False and "역할" in r["error"]
    _reports(ctx, roles=("technical",))
    r = tools.submit_fo_decision(ctx, date=D, payload=_env(ctx))
    assert r["ok"] is False and "역할" in r["error"]
    _reports(ctx, roles=("sentiment",))
    r = tools.submit_fo_decision(ctx, date=D, payload=_env(ctx))
    assert r["ok"] is False and "r0" in r["error"]
    _debate(ctx)
    r = tools.submit_fo_decision(ctx, date=D, payload=_env(ctx))
    assert r["ok"] is False and "_fo_summary" in r["error"]
    assert "_fo_decision" not in list_checkpoints(market_dir(D, "KR"))


def test_submit_morning_executes_in_window_and_records(ctx, conn):
    _ready(ctx)
    bad = tools.submit_fo_decision(ctx, date=D, payload={"decision": {"intent": "OPEN"}})
    assert bad["ok"] is False and "스키마" in bad["error"]
    r = tools.submit_fo_decision(ctx, date=D, payload=_env(ctx))
    assert r["ok"] and r["stage"] == "_fo_decision" and r["executed"] is True
    dec = read_checkpoint(market_dir(D, "KR"), "_fo_decision")
    res = dec["data"]["resolved"]
    assert (res["code"], res["kind"], res["index_at_decision"]) == ("105W11", "FUTURES", "350")
    assert dec["call"]["agent"] == "fo-trader"
    row = conn.execute("SELECT * FROM fo_decisions").fetchone()
    assert (row["intent"], row["direction"], row["round"]) == ("OPEN", "LONG", 0)
    assert row["account_id"] is None and row["index_at_decision"] == "350"
    assert json.loads(row["data"])["decision"]["stop_pt"] == 345.0
    stages = list_checkpoints(market_dir(D, "KR"))
    assert f"_fo_risk_check_{ACCT}" in stages and f"_fo_order_{ACCT}" in stages
    assert store.open_positions(conn, ACCT)[0].status == "entering"
    assert r["executions"][ACCT][0]["status"] == "accepted"


def test_submit_envelope_mismatch_rejected(ctx):
    _ready(ctx)
    bad = tools.submit_fo_decision(ctx, date=D, payload={**_env(ctx), "account_id": ACCT})
    assert bad["ok"] is False and "account_id" in bad["error"]
    bad = tools.submit_fo_decision(ctx, date=D, payload={**_env(ctx), "request_id": "other"})
    assert bad["ok"] is False and "request_id" in bad["error"]


def test_submit_resolves_option_code_and_index(ctx, conn):
    _ready(ctx)
    env = _env(ctx, vehicle="OPTION_BUY", direction="SHORT", strike_rule="OTM1", entry_price=5.0,
               target_leverage=0.5)  # fmt: skip
    r = tools.submit_fo_decision(ctx, date=D, payload=env)
    assert r["ok"], r
    res = read_checkpoint(market_dir(D, "KR"), "_fo_decision")["data"]["resolved"]
    assert res["kind"] == "PUT" and res["code"].startswith("305W11") and res["strike"] == "348"
    assert res["multiplier"] == 50000 and res["days_left"] is not None


def test_submit_stale_inputs_records_only(project, conn):
    old = T0 - timedelta(seconds=settings().fo.max_input_age_s + 1)
    fo, inp = _fo(conn, asof=old)
    ctx = tools.ToolContext(settings=fo.settings, accounts=[], conn=conn, now=lambda: T0, fo=fo)
    ctx._inputs = inp  # type: ignore[attr-defined]
    _ready(ctx)
    r = tools.submit_fo_decision(ctx, date=D, payload=_env(ctx))
    assert r["ok"] and r["executed"] is False and "노후" in r["note"]
    rc = read_checkpoint(market_dir(D, "KR"), f"_fo_risk_check_{ACCT}")
    assert rc["data"]["approved"] is False and "입력 노후 — 실행 안 함" in rc["data"]["note"]
    assert store.open_positions(conn, ACCT) == []
    assert control.get_flag(conn, f"fo_pending_entry:{ACCT}") is None


def test_submit_idempotent(ctx, conn):
    _ready(ctx)
    first = tools.submit_fo_decision(ctx, date=D, payload=_env(ctx))
    again = tools.submit_fo_decision(ctx, date=D, payload=_env(ctx, direction="SHORT"))
    assert again["ok"] and again["already"] is True and again["direction"] == "LONG"
    assert conn.execute("SELECT COUNT(*) AS n FROM fo_decisions").fetchone()["n"] == 1
    assert len(ctx.fo.brokers[ACCT].orders) == 1 and first["executed"]


def test_submit_outside_entry_window_stores_pending(project, conn):
    early = T0.replace(hour=23, minute=20) - timedelta(days=1)  # 08:20 KST
    fo, inp = _fo(conn, now=early)
    ctx = tools.ToolContext(settings=fo.settings, accounts=[], conn=conn, now=lambda: early, fo=fo)
    ctx._inputs = inp  # type: ignore[attr-defined]
    _ready(ctx)
    r = tools.submit_fo_decision(ctx, date=D, payload=_env(ctx))
    assert r["ok"] and r["executed"] is False and r["pending"] == [ACCT]
    flag = control.get_flag(conn, f"fo_pending_entry:{ACCT}")
    assert flag["date"] == D.isoformat() and flag["envelope"]["decision"]["direction"] == "LONG"
    assert flag["stage"] == "_fo_decision"
    assert f"_fo_risk_check_{ACCT}" not in list_checkpoints(market_dir(D, "KR"))
    assert ctx.fo.brokers[ACCT].orders == []
    # FLAT·HOLD 는 보류할 것이 없다
    r2 = tools.submit_fo_decision(ctx, date=D, payload=_env(ctx), stage="_fo_decision")
    assert r2["already"]


def test_submit_flat_hold_records_without_broker(ctx, conn):
    _ready(ctx)
    env = _env(ctx, intent="HOLD", direction="FLAT", target_leverage=0.0, stop_pt=None,
               entry_price=None, take_profit_pt=None)  # fmt: skip
    r = tools.submit_fo_decision(ctx, date=D, payload=env)
    assert r["ok"] and r["executed"] is False and r["direction"] == "FLAT"
    rc = read_checkpoint(market_dir(D, "KR"), f"_fo_risk_check_{ACCT}")
    assert rc["data"]["approved"] is True and "관망" in rc["data"]["note"]
    assert ctx.fo.brokers[ACCT].calls == [] or "place_order" not in ctx.fo.brokers[ACCT].calls


def test_reeval_request_and_submit_round(ctx, conn):
    """재평가: 파이썬이 `_fo_reeval_r<n>_<acct>` 요청을 남기고, 제출 봉투는 그 요청(계좌·포지션·
    버전·request_id)과 같아야 하며 포지션 버전이 바뀌었으면 거부한다."""
    _ready(ctx)
    assert tools.submit_fo_decision(ctx, date=D, payload=_env(ctx))["ok"]
    pos = store.open_positions(conn, ACCT)[0]
    req = tools.fo_reeval_request(
        ctx, date=D, account_id=ACCT, position_id=pos.id, trigger={"kind": "move", "detail": {}}
    )
    assert req["ok"] and req["round"] == 1 and req["stage"] == f"_fo_reeval_r1_{ACCT}"
    position = req["position"]
    assert position["expected_version"] == pos.version and position["request_id"]
    b = tools.fo_bundle(ctx, date=D, kind="trader", position_id=pos.id, account=ACCT)
    assert b["ok"] and b["data"]["context"]["position"]["id"] == pos.id
    assert b["data"]["trigger"]["kind"] == "move" and b["data"]["next_round"] == 1
    assert b["data"]["request_id"] == position["request_id"]
    env = {
        "decision": _env(ctx, intent="HOLD")["decision"], "request_id": position["request_id"],
        "account_id": ACCT, "position_id": pos.id, "expected_version": pos.version,
    }  # fmt: skip
    stage = f"_fo_decision_r1_{ACCT}"
    wrong = tools.submit_fo_decision(
        ctx, date=D, payload={**env, "expected_version": 99}, stage=stage
    )
    assert wrong["ok"] is False and "expected_version" in wrong["error"]
    wrong2 = tools.submit_fo_decision(ctx, date=D, payload={**env, "position_id": 77}, stage=stage)
    assert wrong2["ok"] is False
    applied = []
    ctx.fo.reeval_apply = lambda e, r: applied.append((e.position_id, r)) or {"applied": True}
    ok = tools.submit_fo_decision(ctx, date=D, payload=env, stage=stage)
    assert ok["ok"] and ok["round"] == 1 and ok["applied"] == {"applied": True}
    assert applied == [(pos.id, 1)]
    row = conn.execute("SELECT * FROM fo_decisions WHERE round=1").fetchone()
    assert (row["account_id"], row["position_id"], row["intent"]) == (ACCT, pos.id, "HOLD")
    # 버전이 바뀐 뒤 같은 요청의 재제출은 거부
    with store.tx(conn):
        store.transition(conn, pos.id, expected_version=pos.version, note="x")
    req2 = tools.fo_reeval_request(
        ctx, date=D, account_id=ACCT, position_id=pos.id, trigger={"kind": "news"}
    )
    stage2 = f"_fo_decision_r2_{ACCT}"
    stale = tools.submit_fo_decision(
        ctx, date=D, payload={**env, "request_id": req2["position"]["request_id"]}, stage=stage2
    )
    assert stale["ok"] is False and "버전" in stale["error"]


def test_cli_tool_fo_commands_emit_json(project, conn, monkeypatch):
    from typer.testing import CliRunner

    from trader import cli

    fo, inp = _fo(conn)
    c = tools.ToolContext(settings=fo.settings, accounts=[], conn=conn, now=lambda: T0, fo=fo)
    monkeypatch.setattr(cli, "_tool_ctx", lambda: c)
    r = CliRunner().invoke(
        cli.app, ["tool", "fo", "bundle", "--market", "KR", "--date", D.isoformat(), "--kind",
                  "analyst", "--role", "technical"],
    )  # fmt: skip
    assert r.exit_code == 0, r.output
    assert json.loads(r.output.strip().splitlines()[-1])["data"]["role"] == "technical"
    rep = json.dumps(ok_report(ticker="KOSPI200", role="technical"))
    w = CliRunner().invoke(
        cli.app, ["tool", "fo", "write", "--market", "KR", "--date", D.isoformat(), "--stage",
                  "_fo_analyst_technical", "--agent", "analyst-technical", "--json", rep],
    )  # fmt: skip
    assert w.exit_code == 0 and json.loads(w.output.strip().splitlines()[-1])["ok"]
    s = CliRunner().invoke(
        cli.app, ["tool", "fo", "summarize", "--market", "KR", "--date", D.isoformat()]
    )
    assert s.exit_code == 1 and json.loads(s.output.strip().splitlines()[-1])["ok"] is False
    sub = CliRunner().invoke(
        cli.app, ["tool", "submit-fo-decision", "--market", "KR", "--date", D.isoformat(), "--json",
                  json.dumps({"decision": {"intent": "HOLD", "direction": "FLAT",
                                           "vehicle": "FUTURES"}, "request_id": "x"})],
    )  # fmt: skip
    assert sub.exit_code == 1 and "역할" in json.loads(sub.output.strip().splitlines()[-1])["error"]
