"""Phase 26 단계 3 — GitHub Codex PR #61 리뷰 반영(G3-1~4)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from helpers_sources import T0
from orchestrator.test_tools_fo import ACCT, D, _env, _fo, _ready

from fo.test_executor import _held_position
from fo.test_inputs import _ctx as fo_ctx
from fo.test_review_s3 import _accounts_yaml, _fake_builders, _two_accounts
from trader import cli
from trader.fo import executor
from trader.fo import inputs as inputs_mod
from trader.fo import session as fo_session
from trader.orchestrator import tools
from trader.orchestrator.run_store import list_checkpoints, market_dir, read_checkpoint


def _tool_ctx(conn, fo, inp, now):
    ctx = tools.ToolContext(settings=fo.settings, accounts=[], conn=conn, now=lambda: now, fo=fo,
                            dry_run=False)  # fmt: skip
    ctx._inputs = inp  # type: ignore[attr-defined]
    return ctx


# --- G3-1 제출 시점 신선도 -------------------------------------------------------------------


def test_late_submit_records_only(project, conn):
    fo, inp = _fo(conn)  # 입력 asof = T0
    ctx = _tool_ctx(conn, fo, inp, T0 + timedelta(hours=2))
    _ready(ctx)
    r = tools.submit_fo_decision(ctx, date=D, payload=_env(ctx))
    assert r["ok"] and r["executed"] is False and tools.FO_SUBMIT_STALE_NOTE in r["note"]
    rc = read_checkpoint(market_dir(D, "KR"), f"_fo_risk_check_{ACCT}")
    assert rc["data"]["record_only"] == "stale_submit"
    assert fo.brokers[ACCT].orders == []


def test_submit_within_session_length_executes(project, conn):
    fo, inp = _fo(conn)
    ctx = _tool_ctx(conn, fo, inp, T0 + timedelta(seconds=fo.settings.llm.timeout_s))
    _ready(ctx)
    r = tools.submit_fo_decision(ctx, date=D, payload=_env(ctx))
    assert r["executed"] is True and len(fo.brokers[ACCT].orders) == 1


# --- G3-2 재개는 원장 다리로 판정 ------------------------------------------------------------


def _crash_after_rc(monkeypatch, acct_id):
    """판정 파일만 쓰고 프로세스가 죽은 것처럼(다리 예약 전) 흉내 낸다."""
    real = executor.run_leg

    def crash(fo, acct, env, **kw):
        if acct.id != acct_id:
            return real(fo, acct, env, **kw)
        executor._ckpt(fo, D, f"_fo_risk_check_{acct.id}",
                       {"verdict": {"approved": True, "legs": [{"intent": "OPEN"}]},
                        "record_only": None, "request_id": env.request_id})  # fmt: skip
        return [{"status": "crashed"}]

    monkeypatch.setattr(executor, "run_leg", crash)
    return real


def test_resume_reruns_account_whose_risk_check_has_no_leg(project, conn, monkeypatch):
    ctx = _two_accounts(conn)
    real = _crash_after_rc(monkeypatch, ACCT)
    first = tools.submit_fo_decision(ctx, date=D, payload=_env(ctx))
    assert first["executions"]["paper-fo2"][0]["status"] == "accepted"
    assert ctx.fo.brokers[ACCT].orders == []
    monkeypatch.setattr(executor, "run_leg", real)
    again = tools.submit_fo_decision(ctx, date=D, payload=_env(ctx))
    assert again["resumed_execution"] is True and list(again["executions"]) == [ACCT]
    assert len(ctx.fo.brokers[ACCT].orders) == 1 and len(ctx.fo.brokers["paper-fo2"].orders) == 1
    # 다리가 생긴 뒤에는 판정 파일을 지워도 다시 내지 않는다(원장 다리가 근거)
    (market_dir(D, "KR") / f"_fo_risk_check_{ACCT}.json").unlink()
    third = tools.submit_fo_decision(ctx, date=D, payload=_env(ctx))
    assert third.get("resumed_execution") is not True
    assert len(ctx.fo.brokers[ACCT].orders) == 1


def test_resume_hours_later_records_only(project, conn, monkeypatch):
    ctx = _two_accounts(conn)
    real = _crash_after_rc(monkeypatch, ACCT)
    tools.submit_fo_decision(ctx, date=D, payload=_env(ctx))
    monkeypatch.setattr(executor, "run_leg", real)
    ctx.now = lambda: T0 + timedelta(hours=3)
    again = tools.submit_fo_decision(ctx, date=D, payload=_env(ctx))
    assert again["resumed_execution"] is True and again["executed"] is False
    rc = read_checkpoint(market_dir(D, "KR"), f"_fo_risk_check_{ACCT}")
    assert rc["data"]["record_only"] == "stale_submit"
    assert ctx.fo.brokers[ACCT].orders == []


# --- G3-3 거시 절 시크릿 전달 ----------------------------------------------------------------


def test_news_macro_forwards_secrets(conn, monkeypatch):
    from trader.orchestrator import market_view

    seen: dict = {}

    def fake_build(conn_, market, today, **kw):
        seen["secrets"] = kw.get("secrets")
        return {"errors": {}}

    monkeypatch.setattr(market_view, "build_inputs", fake_build)
    ctx = fo_ctx(conn)
    marker = object()
    ctx.secrets = marker
    inputs_mod.build_inputs(ctx, today=D, asof=T0, accounts=ctx.accounts)
    assert seen["secrets"] is marker


def test_session_run_puts_secrets_on_context(conn, monkeypatch):
    seen: dict = {}

    def capture(ctx, **kw):
        seen["secrets"] = ctx.secrets
        raise RuntimeError("stop")

    monkeypatch.setattr(fo_session.inputs_mod, "build_inputs", capture)
    fo = fo_ctx(conn)
    marker = object()
    with pytest.raises(RuntimeError, match="stop"):
        fo_session.run(fo.settings, conn, fo_accounts=fo.accounts, brokers=fo.brokers,
                       market_client=fo.market_client, master=fo.master, now=lambda: T0,
                       secrets=marker)  # fmt: skip
    assert seen["secrets"] is marker


def test_fo_tool_ctx_carries_boot_secrets(project, conn, monkeypatch):
    _accounts_yaml(project)
    _fake_builders(monkeypatch)
    ctx = cli._tool_ctx(fo=True)
    assert ctx.fo is not None and ctx.fo.secrets is not None


# --- G3-4 보유 축소는 진입 창 밖에서도 ---------------------------------------------------------

LATE = T0.replace(hour=6, minute=35)  # 15:35 KST — 진입 cutoff(15:30) 뒤, 장(15:45) 안


def test_replace_with_position_closes_after_entry_cutoff(project, conn):
    fo, inp = _fo(conn, now=LATE)
    _held_position(conn, fo, qty=2)
    ctx = _tool_ctx(conn, fo, inp, LATE)
    _ready(ctx)
    r = tools.submit_fo_decision(ctx, date=D, payload=_env(ctx, intent="REPLACE"))
    assert r["pending"] == [] and r["executions"][ACCT][0]["status"] == "accepted", r
    req = fo.brokers[ACCT].orders[0]
    assert (req.side, req.qty) == ("SELL", 2)


def test_resize_with_position_runs_open_without_position_pends(project, conn, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(executor, "run_leg",
                        lambda fo, acct, env, **kw: calls.append(env.decision.intent) or
                        [{"status": "accepted"}])  # fmt: skip
    fo, inp = _fo(conn, now=LATE)
    _held_position(conn, fo, qty=3)
    ctx = _tool_ctx(conn, fo, inp, LATE)
    _ready(ctx)
    r = tools.submit_fo_decision(ctx, date=D, payload=_env(ctx, intent="RESIZE",
                                                          target_leverage=0.2))  # fmt: skip
    assert calls == ["RESIZE"] and r["pending"] == []


def test_open_after_entry_cutoff_still_pends(project, conn):
    fo, inp = _fo(conn, now=LATE)
    ctx = _tool_ctx(conn, fo, inp, LATE)
    _ready(ctx)
    r = tools.submit_fo_decision(ctx, date=D, payload=_env(ctx))
    assert r["pending"] == [ACCT] and fo.brokers[ACCT].orders == []
    assert f"_fo_risk_check_{ACCT}" not in list_checkpoints(market_dir(D, "KR"))
