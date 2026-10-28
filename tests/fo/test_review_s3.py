"""Phase 26 단계 3 교차 리뷰 반영(Codex exec C1~9·codex review P1·Claude 1~14): 국면·시장 뷰 연결,
지연 fo 컨텍스트, kind 별 프롬프트, 제출 시 입력 날짜·보류 플래그, 계좌별 예외 격리·재개, KST 분봉
컷, 현물 헤지 보유 전달, 재평가 라운드 고정, 묶음 크기 상한, asof 운영일, 수단 계약 ↔ md."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from helpers_agents import ok_report
from helpers_sources import T0
from orchestrator.test_tools_fo import ACCT, D, _env, _fo, _ready
from typer.testing import CliRunner

from fo._gate import quote
from fo.test_executor import balance, settings
from fo.test_inputs import _candles, _market, _master_with_chain
from fo.test_inputs import _ctx as fo_ctx
from trader import cli
from trader.agents import registry
from trader.agents.registry import orchestrator_prompt
from trader.broker import fo_master
from trader.broker.fake import FakeBroker, FakeFoBroker, FakeMarket
from trader.broker.models import Candle, Cash
from trader.config import KST, AccountConfig, Settings
from trader.control import control
from trader.fo import executor, store
from trader.fo import inputs as inputs_mod
from trader.fo import session as fo_session
from trader.orchestrator import session as session_mod
from trader.orchestrator import tools
from trader.orchestrator.run_store import list_checkpoints, market_dir, read_checkpoint

REPO_AGENTS = Path(__file__).resolve().parents[2] / ".claude" / "agents"


# --- 1. 국면·시장 뷰 연결 ------------------------------------------------------------------


def _accounts_yaml(project, *, with_fo=True):
    import yaml

    rows = [{"id": "paper-main", "env": "paper", "cano": "00000000", "markets": ["KR"],
             "mode": "auto"}]  # fmt: skip
    if with_fo:
        rows.append({"id": "paper-fo", "env": "paper", "cano": "00000000", "acnt_prdt_cd": "03",
                     "markets": ["KR"], "mode": "auto", "asset": "fo"})  # fmt: skip
    (project / "config" / "accounts.yaml").write_text(
        yaml.safe_dump({"accounts": rows}), encoding="utf-8"
    )


def _fake_builders(monkeypatch):
    def brokers(secrets, accounts, *, settings=None):
        out = []
        for a in accounts:
            if a.asset == "fo":
                out.append(FakeFoBroker(a, balance=balance(), now=lambda: T0))
            else:
                out.append(FakeBroker(a, cash={"KR": Cash(currency="KRW", available=1, total=1)}))
        return out

    monkeypatch.setattr(cli, "_build_brokers", brokers)
    monkeypatch.setattr(cli, "_build_market", lambda secrets: _market())
    monkeypatch.setattr(fo_master, "load", lambda **kw: _master_with_chain())


def test_fo_tool_ctx_wires_regime_view_and_spot(project, conn, monkeypatch):
    _accounts_yaml(project)
    _fake_builders(monkeypatch)
    control.set_flag(conn, "regime:KR", {"state": "bear", "count": 3})
    today = datetime.now(KST).date().isoformat()  # 도구 컨텍스트의 시계는 실제 now
    control.set_flag(conn, "market_view:KR", {"date": today, "stance": "RISK_OFF",
                                              "confidence": 0.8})  # fmt: skip
    conn.commit()
    monkeypatch.setenv("TRADER_DRY_RUN", "0")
    ctx = cli._tool_ctx(fo=True)
    fo = ctx.fo
    assert fo is not None and fo.regime_of() == "bear"
    assert fo.view_of()["stance"] == "RISK_OFF"
    assert [a.id for a in fo.spot_accounts] == ["paper-main"]
    assert isinstance(fo.spot_brokers["paper-main"], FakeBroker)
    assert fo.dry_run is False and fo.historical is False


def test_tool_ctx_builds_fo_only_on_request(project, conn, monkeypatch):
    _accounts_yaml(project)
    _fake_builders(monkeypatch)
    calls: list[str] = []
    orig = cli._fo_tool_ctx

    def spy(*a, **kw):
        calls.append("fo")
        return orig(*a, **kw)

    monkeypatch.setattr(cli, "_fo_tool_ctx", spy)
    assert cli._tool_ctx().fo is None and calls == []
    assert cli._tool_ctx(fo=True).fo is not None and calls == ["fo"]


def test_fo_tool_ctx_none_without_fo_accounts_and_master_failure_empty(project, conn, monkeypatch):
    _accounts_yaml(project, with_fo=False)
    _fake_builders(monkeypatch)
    assert cli._tool_ctx(fo=True).fo is None
    _accounts_yaml(project)

    def boom(**kw):
        raise OSError("no net")

    monkeypatch.setattr(fo_master, "load", boom)
    assert cli._tool_ctx(fo=True).fo.master == []


def test_master_load_offline_never_downloads(tmp_path):
    class NoHttp:
        def get(self, *a, **k):
            raise AssertionError("도구 경로는 다운로드하지 않는다")

    assert fo_master.load(cache_dir=tmp_path, http=NoHttp(), offline=True) == []


def test_session_run_wires_regime_view(project, conn, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE", "1")
    control.set_flag(conn, "regime:KR", {"state": "bull", "count": 0})
    seen: dict = {}

    def runner(spec, settings, conn_, accounts, *, fo=None, **kw):
        seen["fo"] = fo
        return session_mod.run_session(spec, settings, conn_, accounts, fo=fo, **kw)

    from fo.test_session import _deps

    out = fo_session.run(**_deps(conn), session_runner=runner)
    assert out["session"]["ok"]
    assert seen["fo"].regime_of() == "bull"
    assert seen["fo"].view_of() is None


# --- 2. kind 별 오케스트레이터 프롬프트 -----------------------------------------------------


def test_orchestrator_prompt_branches_by_kind():
    spot = orchestrator_prompt(Settings(), REPO_AGENTS)  # 기본 analyze
    assert "FoDecisionEnvelope" not in spot and "## fo 모드" not in spot
    assert "tool submit-decision" in spot and "## screen 모드" not in spot
    fo = orchestrator_prompt(Settings(), REPO_AGENTS, kind="fo")
    assert "FoDecisionEnvelope" in fo and "## fo 모드" in fo and "## fo_resume 모드" not in fo
    assert "## screen 모드" not in fo and "# 절차 (종목마다" not in fo
    assert "- Decision 스키마" not in fo and "HedgeProposal" not in fo  # 현물 스키마 없음
    resume = orchestrator_prompt(Settings(), REPO_AGENTS, kind="fo_resume")
    assert "## fo_resume 모드" in resume and "## fo 모드" not in resume
    screen = orchestrator_prompt(Settings(), REPO_AGENTS, kind="screen")
    assert "## screen 모드" in screen and "Screened 스키마" in screen and "fo" not in screen.lower()


def test_run_session_passes_kind_to_prompt():
    import inspect

    src = inspect.getsource(session_mod.run_session)
    assert "registry.orchestrator_prompt(settings, agents_dir, kind=spec.kind)" in src


# --- 3. 제출 시 입력 날짜·보류 플래그·dry_run -----------------------------------------------


def test_submit_rejects_inputs_from_other_run_date(project, conn):
    fo, inp = _fo(conn)
    other = inputs_mod.build_inputs(fo, today=D - timedelta(days=1), asof=T0, accounts=fo.accounts)
    inputs_mod.save_inputs(other, today=D, state=None)  # 어제 입력이 오늘 디렉터리에
    ctx = tools.ToolContext(settings=fo.settings, accounts=[], conn=conn, now=lambda: T0, fo=fo)
    ctx._inputs = other  # type: ignore[attr-defined]
    _ready(ctx)
    r = tools.submit_fo_decision(ctx, date=D, payload=_env(ctx))
    assert r["ok"] and r["executed"] is False and "날짜" in r["note"]
    rc = read_checkpoint(market_dir(D, "KR"), f"_fo_risk_check_{ACCT}")
    assert rc["data"]["approved"] is False and rc["data"]["record_only"] == "input_date"
    assert store.open_positions(conn, ACCT) == []


def test_pending_flag_skipped_in_dry_run_and_consumer_requires_today(project, conn):
    early = T0.replace(hour=23, minute=20) - timedelta(days=1)  # 08:20 KST
    fo, inp = _fo(conn, now=early)
    fo.dry_run = True
    ctx = tools.ToolContext(settings=fo.settings, accounts=[], conn=conn, now=lambda: early, fo=fo,
                            dry_run=True)  # fmt: skip
    ctx._inputs = inp  # type: ignore[attr-defined]
    _ready(ctx)
    # 어제의 낡은 보류 플래그가 남아 있어도 오늘 제출이 지운다
    key = f"{tools.FO_PENDING_FLAG}{ACCT}"
    control.set_flag(conn, key, {"date": (D - timedelta(days=1)).isoformat(), "envelope": {}})
    r = tools.submit_fo_decision(ctx, date=D, payload=_env(ctx))
    assert r["ok"] and r["executed"] is False and r["pending"] == []
    assert control.get_flag(conn, key) is None
    rc = read_checkpoint(market_dir(D, "KR"), f"_fo_risk_check_{ACCT}")
    assert "dry_run" in rc["data"]["note"] and rc["data"]["record_only"] == "dry_run"
    # 소비자: 오늘 것만 돌려주고 낡은 것은 지운다
    yesterday = (D - timedelta(days=1)).isoformat()
    control.set_flag(conn, key, {"date": yesterday, "envelope": {"x": 1}})
    assert tools.fo_pending_entry(conn, ACCT, D) is None
    assert control.get_flag(conn, key) is None
    control.set_flag(conn, key, {"date": D.isoformat(), "envelope": {"x": 1}, "stage": "x"})
    assert tools.fo_pending_entry(conn, ACCT, D)["envelope"] == {"x": 1}


def test_pending_flag_records_requested_context(project, conn):
    early = T0.replace(hour=23, minute=20) - timedelta(days=1)
    fo, inp = _fo(conn, now=early)
    ctx = tools.ToolContext(settings=fo.settings, accounts=[], conn=conn, now=lambda: early, fo=fo)
    ctx._inputs = inp  # type: ignore[attr-defined]
    _ready(ctx)
    r = tools.submit_fo_decision(ctx, date=D, payload=_env(ctx))
    assert r["pending"] == [ACCT]
    flag = control.get_flag(conn, f"{tools.FO_PENDING_FLAG}{ACCT}")
    assert flag["dry_run"] is False and flag["request_id"] == inp["meta"]["request_id"]


# --- 4. 계좌별 예외 격리·재개 ---------------------------------------------------------------


def _two_accounts(conn):
    fo, inp = _fo(conn)
    second = AccountConfig(id="paper-fo2", env="paper", cano="00000000", acnt_prdt_cd="03",
                           markets=["KR"], mode="auto", asset="fo")  # fmt: skip
    fo.accounts.append(second)
    fo.brokers[second.id] = FakeFoBroker(second, balance=balance(), now=lambda: T0,
                                         orderable=fo.brokers[ACCT].orderable_result)  # fmt: skip
    from trader.broker.fo import CONTRACT_VERSION

    for v in ("FUTURES", "OPTION_BUY", "OPTION_SELL"):
        control.set_flag(conn, executor.verified_key(fo.settings, second, v), CONTRACT_VERSION)
    ctx = tools.ToolContext(settings=fo.settings, accounts=[], conn=conn, now=lambda: T0, fo=fo,
                            dry_run=False)  # fmt: skip
    ctx._inputs = inp  # type: ignore[attr-defined]
    _ready(ctx)
    return ctx


def test_submit_isolates_account_exception_then_resumes(project, conn, monkeypatch):
    ctx = _two_accounts(conn)
    real = executor.run_leg

    def flaky(fo, acct, env, **kw):
        if acct.id == ACCT:
            raise RuntimeError("broker down")
        return real(fo, acct, env, **kw)

    monkeypatch.setattr(executor, "run_leg", flaky)
    r = tools.submit_fo_decision(ctx, date=D, payload=_env(ctx))
    assert r["ok"] and r["executions"]["paper-fo2"][0]["status"] == "accepted"
    assert "broker down" in r["executions"][ACCT][0]["error"]
    rc = read_checkpoint(market_dir(D, "KR"), f"_fo_risk_check_{ACCT}")
    assert "RuntimeError" in rc["data"]["error"] and rc["data"]["approved"] is False
    assert store.open_positions(conn, ACCT) == [] and store.open_positions(conn, "paper-fo2")
    # 같은 결정 재제출: 판정이 없는 계좌만 재개(이미 접수된 계좌는 다시 내지 않는다)
    monkeypatch.setattr(executor, "run_leg", real)
    (market_dir(D, "KR") / f"_fo_risk_check_{ACCT}.json").unlink()
    again = tools.submit_fo_decision(ctx, date=D, payload=_env(ctx))
    assert again["already"] is True and again["resumed_execution"] is True
    assert list(again["executions"]) == [ACCT]
    assert len(ctx.fo.brokers["paper-fo2"].orders) == 1 and len(ctx.fo.brokers[ACCT].orders) == 1
    third = tools.submit_fo_decision(ctx, date=D, payload=_env(ctx))
    assert third["already"] is True and third.get("resumed_execution") is not True


def test_submit_bookkeeping_error_is_critical_and_continues(project, conn, monkeypatch, caplog):
    import logging

    from trader.orchestrator.executor import OrderBookkeepingError

    ctx = _two_accounts(conn)
    real = executor.run_leg

    def boom(fo, acct, env, **kw):
        if acct.id == ACCT:
            raise OrderBookkeepingError("ODN1", RuntimeError("db locked"))
        return real(fo, acct, env, **kw)

    monkeypatch.setattr(executor, "run_leg", boom)
    with caplog.at_level(logging.CRITICAL, logger="trader.orchestrator"):
        r = tools.submit_fo_decision(ctx, date=D, payload=_env(ctx))
    assert r["executions"]["paper-fo2"][0]["status"] == "accepted"
    assert r["executions"][ACCT][0]["order_no"] == "ODN1"
    assert any(rec.levelno == logging.CRITICAL for rec in caplog.records)
    rc = read_checkpoint(market_dir(D, "KR"), f"_fo_risk_check_{ACCT}")
    assert rc["data"]["order_no"] == "ODN1" and rc["data"]["approved"] is False
    # 재제출은 판정 파일이 있는 계좌(접수번호 있음)를 재개하지 않는다 — 원장 기록 실패는 사람이 대조
    monkeypatch.setattr(executor, "run_leg", real)
    again = tools.submit_fo_decision(ctx, date=D, payload=_env(ctx))
    assert again["already"] and again.get("resumed_execution") is not True
    assert ctx.fo.brokers[ACCT].orders == []


# --- 5. KST 분봉·일봉 컷 --------------------------------------------------------------------


def test_candles_cut_by_kst_run_date_and_minute_time(conn):
    asof = datetime(2026, 10, 27, 0, 30, tzinfo=KST)  # 10-26 15:30 UTC — KST 날짜는 10-27
    day = asof.date()
    daily = _candles(5)[:-1] + [
        Candle(date=day, open=Decimal(350), high=Decimal(351), low=Decimal(349),
               close=Decimal(350), volume=10)  # fmt: skip
    ]
    minutes = [
        Candle(date=day, open=Decimal(350), high=Decimal(350), low=Decimal(350),
               close=Decimal(350), volume=1, time=t)  # fmt: skip
        for t in ("090000", "003000", "103000")
    ] + [
        Candle(date=day - timedelta(days=1), open=Decimal(350), high=Decimal(350),
               low=Decimal(350), close=Decimal(350), volume=1, time="150000")  # fmt: skip
    ]
    market = FakeMarket(
        candles={"069500": _candles()}, fo_quotes={"105W11": quote()},
        fo_candles={"105W11": daily}, fo_minutes={"105W11": minutes},
        kospi200=(Decimal("350"), asof), now=lambda: asof,
    )  # fmt: skip
    ctx = fo_ctx(conn, market=market, now=asof)
    inp = inputs_mod.build_inputs(ctx, today=day, asof=asof, accounts=ctx.accounts)
    tech = inp["technical"]["data"]
    assert tech["meta"]["last_candle_date"] == day.isoformat()  # UTC 날짜(10-26)로 자르지 않음
    kept = sorted((c["date"], c["time"]) for c in tech["minute"]["data"])
    assert kept == [((day - timedelta(days=1)).isoformat(), "150000"), (day.isoformat(), "003000")]


# --- 6. 현물 헤지 보유는 현물 계좌·브로커로 ---------------------------------------------------


def test_news_macro_receives_spot_accounts(conn, monkeypatch):
    from trader.orchestrator import market_view

    seen: dict = {}

    def fake_build(conn_, market, today, **kw):
        seen["accounts"] = [a.id for a in kw.get("accounts") or []]
        seen["brokers"] = sorted(kw.get("brokers") or {})
        return {"hedge": {"held": ["114800"]}, "errors": {}}

    monkeypatch.setattr(market_view, "build_inputs", fake_build)
    spot = AccountConfig(id="paper-main", env="paper", cano="00000000", markets=["KR"], mode="auto")
    ctx = fo_ctx(conn)
    ctx.spot_accounts = [spot]
    ctx.spot_brokers = {spot.id: object()}
    inp = inputs_mod.build_inputs(ctx, today=D, asof=T0, accounts=ctx.accounts)
    assert seen == {"accounts": ["paper-main"], "brokers": ["paper-main"]}
    assert inp["context"]["data"]["spot_exposure"]["hedge_held"] == ["114800"]


# --- 7. 재평가 라운드 고정·요청 재사용 --------------------------------------------------------


def test_reeval_request_reused_until_decided_and_bundle_round_pinned(project, conn):
    fo, inp = _fo(conn)
    ctx = tools.ToolContext(settings=fo.settings, accounts=[], conn=conn, now=lambda: T0, fo=fo,
                            dry_run=False)  # fmt: skip
    ctx._inputs = inp  # type: ignore[attr-defined]
    _ready(ctx)
    assert tools.submit_fo_decision(ctx, date=D, payload=_env(ctx))["ok"]
    pos = store.open_positions(conn, ACCT)[0]
    r1 = tools.fo_reeval_request(ctx, date=D, account_id=ACCT, position_id=pos.id,
                                 trigger={"kind": "move"})  # fmt: skip
    r1b = tools.fo_reeval_request(ctx, date=D, account_id=ACCT, position_id=pos.id,
                                  trigger={"kind": "news"})  # fmt: skip
    assert r1["round"] == 1 and r1b["round"] == 1 and r1b["reused"] is True
    assert r1b["position"]["request_id"] == r1["position"]["request_id"]
    assert r1b["position"]["trigger"] == {"kind": "move"}  # 먼저 든 요청이 정본
    b = tools.fo_bundle(ctx, date=D, kind="trader", position_id=pos.id, account=ACCT, round=1)
    assert b["ok"] and b["data"]["request_id"] == r1["position"]["request_id"]
    missing = tools.fo_bundle(ctx, date=D, kind="trader", position_id=pos.id, account=ACCT, round=5)
    assert missing["ok"] is False and "_fo_reeval_r5" in missing["error"]
    # 결정이 들어오면 다음 요청은 새 라운드
    keys = ("request_id", "account_id", "position_id", "expected_version")
    env = {"decision": _env(ctx, intent="HOLD")["decision"], **{k: r1["position"][k] for k in keys}}
    stage1 = f"_fo_decision_r1_{ACCT}"
    assert tools.submit_fo_decision(ctx, date=D, payload=env, stage=stage1)["ok"]
    r2 = tools.fo_reeval_request(ctx, date=D, account_id=ACCT, position_id=pos.id,
                                 trigger={"kind": "news"})  # fmt: skip
    assert r2["round"] == 2 and not r2.get("reused")
    # 버전이 바뀌면 미결 요청이 있어도 새 라운드(낡은 요청의 제출은 버전 불일치로 거부된다)
    with store.tx(conn):
        store.transition(conn, pos.id, expected_version=pos.version, note="x")
    r3 = tools.fo_reeval_request(ctx, date=D, account_id=ACCT, position_id=pos.id,
                                 trigger={"kind": "news"})  # fmt: skip
    assert r3["round"] == 3 and not r3.get("reused")


# --- 8. 묶음 크기 상한 -----------------------------------------------------------------------


def test_bundle_size_capped_with_long_candle_lists(project, conn):
    minutes = [
        Candle(date=D, open=Decimal(350), high=Decimal(351), low=Decimal(349), close=Decimal(350),
               volume=100 + i, time=f"{9 + i // 60:02d}{i % 60:02d}00")  # fmt: skip
        for i in range(380)
    ]
    market = FakeMarket(
        candles={"069500": _candles()}, fo_quotes={"105W11": quote()},
        fo_candles={"105W11": _candles(400)}, fo_minutes={"105W11": minutes},
        kospi200=(Decimal("350"), T0), now=lambda: T0,
    )  # fmt: skip
    fo = fo_ctx(conn, market=market)
    fo.settings = settings(enabled=True)
    inp = inputs_mod.build_inputs(fo, today=D, asof=T0, accounts=fo.accounts)
    inputs_mod.save_inputs(inp, today=D, state=None)
    assert len(inp["technical"]["data"]["daily"]["data"]) == 260  # sources.yaml kis.daily_days
    raw = inputs_mod.bundle(inp, kind="analyst", role="technical")
    assert tools._size(raw) > tools.BUNDLE_LIMIT_BYTES  # 자르기 전엔 한도 초과
    ctx = tools.ToolContext(settings=fo.settings, accounts=[], conn=conn, now=lambda: T0, fo=fo)
    b = tools.fo_bundle(ctx, date=D, kind="analyst", role="technical")
    assert b["ok"]
    assert tools._size(b["data"]) <= tools.BUNDLE_LIMIT_BYTES
    daily = b["data"]["technical"]["daily"]["data"]
    assert daily[-1]["date"] == inp["technical"]["data"]["daily"]["data"][-1]["date"]  # 꼬리 유지


# --- 9. asof 로 운영일·세션 dry_run 전파 -------------------------------------------------------


def test_session_run_date_from_asof(project, conn, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE", "1")
    from fo.test_session import _deps

    deps = _deps(conn)
    deps.pop("date")
    asof = datetime(2026, 10, 27, 0, 30, tzinfo=KST)  # UTC 로는 10-26
    deps["now"] = lambda: asof
    out = fo_session.run(**deps, asof=asof)
    assert out["session"]["ok"]
    assert "_fo_decision" in list_checkpoints(market_dir(asof.date(), "KR"))
    assert out["date"] == asof.date().isoformat()


def test_run_session_dry_run_forces_fo_dry_run(project, conn, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE", "1")
    fo, inp = _fo(conn)
    assert fo.dry_run is False
    spec = session_mod.SessionSpec(market="KR", date=D, tickers=[], dry_run=True, kind="fo")
    res = session_mod.run_session(spec, fo.settings, conn, [], now=lambda: T0, fo=fo)
    assert res.ok and fo.dry_run is True


# --- 10. 도구 계약·CLI --------------------------------------------------------------------------


def test_fo_write_requires_report_ticker_to_be_proxy(project, conn):
    fo, inp = _fo(conn)
    ctx = tools.ToolContext(settings=fo.settings, accounts=[], conn=conn, now=lambda: T0, fo=fo)
    bad = tools.fo_write(ctx, date=D, stage="_fo_analyst_technical",
                         payload=ok_report(ticker="005930", role="technical"))  # fmt: skip
    assert bad["ok"] is False and "KOSPI200" in bad["error"]


def test_fo_tool_commands_reject_non_kr_market(project, conn, monkeypatch):
    fo, inp = _fo(conn)
    c = tools.ToolContext(settings=fo.settings, accounts=[], conn=conn, now=lambda: T0, fo=fo)
    monkeypatch.setattr(cli, "_tool_ctx", lambda **kw: c)
    for args in (
        ["tool", "fo", "bundle", "--market", "US", "--date", D.isoformat(), "--kind", "analyst",
         "--role", "technical"],
        ["tool", "fo", "summarize", "--market", "US", "--date", D.isoformat()],
        ["tool", "submit-fo-decision", "--market", "US", "--date", D.isoformat(), "--json", "{}"],
    ):  # fmt: skip
        r = CliRunner().invoke(cli.app, args)
        out = json.loads(r.output.strip().splitlines()[-1])
        assert r.exit_code == 1 and out["ok"] is False and "KR" in out["error"], args


def test_fo_trader_md_input_contract_matches_bundle(project, conn):
    fo, inp = _fo(conn)
    ctx = tools.ToolContext(settings=fo.settings, accounts=[], conn=conn, now=lambda: T0, fo=fo,
                            dry_run=False)  # fmt: skip
    ctx._inputs = inp  # type: ignore[attr-defined]
    _ready(ctx)
    assert tools.submit_fo_decision(ctx, date=D, payload=_env(ctx))["ok"]
    pos = store.open_positions(conn, ACCT)[0]
    b = tools.fo_bundle(ctx, date=D, kind="trader", position_id=pos.id, account=ACCT)["data"]
    md = (REPO_AGENTS / "fo-trader.md").read_text(encoding="utf-8")
    assert set(b["context"]["position"]["budget"]) == {"used", "limit", "remaining"}
    assert "budget: {used, limit, remaining}" in md
    assert set(b["context"]["limits"]["option"]["sell_stop_mult"]) == {"min", "default", "max"}
    assert "sell_stop_mult: {min, default, max}" in md
    row, err = inputs_mod._account_summary(fo, fo.accounts[0], today=D)
    assert err is None and row["positions"][0]["id"] == pos.id
    for k in ("id", "code", "kind", "contract_side", "qty_held", "stop_pt", "leverage_used"):
        assert k in row["positions"][0]
    assert set(row["legs"][0]) == {"id", "position_id", "intent", "status", "qty", "filled_qty",
                                   "price"}  # fmt: skip
    assert "positions: [{id, code, kind, contract_side, direction, vehicle, status, qty_held" in md


def test_claude_md_lists_fo_kinds_and_agent_count():
    text = (REPO_AGENTS.parents[1] / "CLAUDE.md").read_text(encoding="utf-8")
    assert "16개 md" in text and "12개 md" not in text
    assert "analyze|screen|resume|reflect|market|discover|fo|fo_resume" in text


def test_registry_fo_agents_include_market_analyst():
    assert "analyst-market" in registry.FO_AGENTS and "fo-trader" in registry.FO_AGENTS
