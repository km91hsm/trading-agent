"""파생 장중 재평가 적용(Phase 26 Task 4.3): 늦은 재평가 CAS·HOLD 단조·RESIZE·CLOSE·REPLACE
다음 틱 진입·라운드 행 1개·대기열 잡 본체."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from orchestrator.test_tools_fo import D, _fo, _ready

from fo.test_executor import ACCT, T0, TODAY, _held_position, decision, env, make_ctx
from trader.control import control
from trader.fo import executor, reeval, store, triggers
from trader.ledger import repo
from trader.orchestrator import tools


def _env(pos, **dec):
    return env(decision(**dec), account_id=ACCT, position_id=pos.id,
               expected_version=pos.version)  # fmt: skip


def test_apply_discards_on_version_mismatch(conn):
    ctx = make_ctx(conn)
    pos = _held_position(conn, ctx, qty=2)
    e = env(decision(intent="CLOSE", direction="FLAT", stop_pt=None, target_leverage=0),
            account_id=ACCT, position_id=pos.id, expected_version=pos.version + 1)  # fmt: skip
    out = reeval.apply(ctx, e, round_n=1)
    assert "discarded" in out and ctx.brokers[ACCT].orders == []


def test_apply_hold_monotone_only(conn):
    ctx = make_ctx(conn)
    pos = _held_position(conn, ctx, qty=2)
    reeval.apply(ctx, _env(pos, intent="HOLD", stop_pt=340.0, take_profit_pt=365.0), round_n=1)
    cur = store.get_position(conn, pos.id)
    assert cur.stop_pt == Decimal("345") and cur.tp_pt == Decimal("365")  # 불리한 손절 이동 거부
    reeval.apply(ctx, _env(cur, intent="HOLD", stop_pt=347.0, take_profit_pt=365.0), round_n=2)
    assert store.get_position(conn, pos.id).stop_pt == Decimal("347")
    assert ctx.brokers[ACCT].orders == []


def test_apply_resize_total(conn):
    ctx = make_ctx(conn, max_contracts=10)
    pos = _held_position(conn, ctx, qty=3, qty_target=3, leverage_used=1.0)
    out = reeval.apply(ctx, _env(pos, intent="RESIZE", target_leverage=0.2), round_n=1)
    assert out["applied"] == "RESIZE" and out["results"][0]["intent"] == "RESIZE_DOWN"
    req = ctx.brokers[ACCT].orders[0]
    assert (req.side, req.qty) == ("SELL", 2)


def test_apply_close(conn):
    ctx = make_ctx(conn)
    pos = _held_position(conn, ctx, qty=2)
    out = reeval.apply(ctx, _env(pos, intent="CLOSE", direction="FLAT", stop_pt=None,
                                 target_leverage=0), round_n=1)  # fmt: skip
    assert out["applied"] == "CLOSE"
    assert (ctx.brokers[ACCT].orders[0].side, ctx.brokers[ACCT].orders[0].qty) == ("SELL", 2)


def test_apply_replace_after_flat_next_tick(conn):
    ctx = make_ctx(conn)
    pos = _held_position(conn, ctx, qty=2)
    out = reeval.apply(ctx, _env(pos, intent="REPLACE", direction="SHORT", stop_pt=355.0,
                                 take_profit_pt=340.0, entry_price=350.5), round_n=1)  # fmt: skip
    assert out["applied"] == "REPLACE" and out["replace_pending"] is True
    broker = ctx.brokers[ACCT]
    assert [(o.side, o.qty) for o in broker.orders] == [("SELL", 2)]
    assert reeval.continue_replace(ctx, executor.account_of(ctx, ACCT)) == []  # 청산 진행 중
    leg = store.live_legs(conn, position_id=pos.id)[0]
    with store.tx(conn):
        store.record_fill_progress(conn, leg.id, 2)
    executor.finalize(ctx, store.get_position(conn, pos.id))
    broker.balance = broker.balance.model_copy(update={"positions": []})
    res = reeval.continue_replace(ctx, executor.account_of(ctx, ACCT))
    assert res and "opened" in res[0]
    assert broker.orders[-1].side == "SELL"  # 선물 숏 진입 = 매도
    new = [p for p in store.open_positions(conn, ACCT) if p.id != pos.id]
    assert new and new[0].direction == "SHORT"
    assert control.get_flag(conn, reeval.replace_key(ACCT, pos.id)) is None
    assert reeval.continue_replace(ctx, executor.account_of(ctx, ACCT)) == []


def test_round_row_unique_via_submit(project, conn):
    fo, inp = _fo(conn)
    pos = _held_position(conn, fo, qty=2)
    ctx = tools.ToolContext(settings=fo.settings, accounts=[], conn=conn, now=lambda: T0, fo=fo,
                            dry_run=False)  # fmt: skip
    ctx._inputs = inp  # type: ignore[attr-defined]
    _ready(ctx)
    req = tools.fo_reeval_request(ctx, date=D, account_id=ACCT, position_id=pos.id,
                                  trigger={"kind": "move"})  # fmt: skip
    p = req["position"]
    payload = {"decision": decision(intent="HOLD", stop_pt=347.0).model_dump(mode="json"),
               "account_id": ACCT, "position_id": pos.id, "expected_version": p["expected_version"],
               "request_id": p["request_id"]}  # fmt: skip
    stage = f"_fo_decision_r{req['round']}_{ACCT}"
    r = tools.submit_fo_decision(ctx, date=D, payload=payload, stage=stage)
    assert r["ok"] and r["executed"] is True and r["applied"]["applied"] == "HOLD"
    assert store.get_position(conn, pos.id).stop_pt == Decimal("347")
    again = tools.submit_fo_decision(ctx, date=D, payload=payload, stage=stage)
    assert again["already"] is True
    n = conn.execute("SELECT COUNT(*) FROM fo_decisions WHERE position_id = ? AND round = ?",
                     (pos.id, req["round"])).fetchone()[0]  # fmt: skip
    assert n == 1


def _ready_fo(conn):
    fo, inp = _fo(conn)
    tctx = tools.ToolContext(settings=fo.settings, accounts=[], conn=conn, now=lambda: T0, fo=fo,
                             dry_run=False)  # fmt: skip
    tctx._inputs = inp  # type: ignore[attr-defined]
    _ready(tctx)
    return fo


def test_run_skips_without_morning_summary(project, conn):
    fo, _ = _fo(conn)  # 입력만 있고 애널리스트·요약 없음
    pos = _held_position(conn, fo, qty=2)
    tid = triggers.record(conn, pos, triggers.Trigger("move", {}, Decimal("365")), now=T0)
    rq = reeval.request(fo, account_id=ACCT, position_id=pos.id, trigger_id=tid,
                        trigger={"kind": "fo:move"}, today=TODAY)  # fmt: skip
    called = []
    out = reeval.run(fo, rq["params"], session_runner=lambda *a, **k: called.append(1))
    assert "skipped" in out and called == []
    row = conn.execute("SELECT session_id FROM triggers WHERE id = ?", (tid,)).fetchone()
    assert row["session_id"].startswith("폐기")


def test_run_marks_trigger_and_skips_changed(project, conn):
    fo = _ready_fo(conn)
    pos = _held_position(conn, fo, qty=2)
    tid = triggers.record(conn, pos, triggers.Trigger("move", {}, Decimal("365")), now=T0)
    rq = reeval.request(fo, account_id=ACCT, position_id=pos.id, trigger_id=tid,
                        trigger={"kind": "fo:move"}, today=D)  # fmt: skip
    seen = {}

    def runner(spec, *a, **kw):
        seen["kind"], seen["position"] = spec.kind, spec.position
        return SimpleNamespace(ok=True, session_id="s-1", error=None)

    out = reeval.run(fo, rq["params"], session_runner=runner)
    assert out.get("ok") and seen["kind"] == "fo_resume", out
    assert seen["position"]["request_id"] == rq["params"]["request_id"]
    assert repo.get_trigger(conn, tid)["handled_at"]
    assert "skipped" in reeval.run(fo, rq["params"], session_runner=runner)  # 이미 처리
    # 포지션 버전이 바뀐 요청은 세션 없이 폐기
    tid2 = triggers.record(conn, pos, triggers.Trigger("news", {}, None), now=T0)
    rq2 = reeval.request(fo, account_id=ACCT, position_id=pos.id, trigger_id=tid2,
                         trigger={"kind": "fo:news"}, today=D)  # fmt: skip
    with store.tx(conn):
        store.transition(conn, pos.id, expected_version=pos.version, note="변경")
    seen.clear()
    out2 = reeval.run(fo, rq2["params"], session_runner=runner)
    assert "skipped" in out2 and seen == {}
    assert repo.get_trigger(conn, tid2)["detail"] is not None
    row = conn.execute("SELECT session_id FROM triggers WHERE id = ?", (tid2,)).fetchone()
    assert row["session_id"].startswith("폐기")
