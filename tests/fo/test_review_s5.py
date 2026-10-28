"""Phase 26 단계 5 전체 교차 리뷰 B 반영(Codex 2·Claude 2 → S5-n)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fo import _gate as g
from fo.test_engine import _market, at
from fo.test_executor import (
    ACCT,
    T0,
    TODAY,
    _held_position,
    account,
    balance,
    decision,
    env,
    make_ctx,
    state,
)
from trader.control import control
from trader.fo import engine, executor, positions, reeval, rules, store
from trader.ledger import repo


def _tick(ctx):
    return engine.tick(ctx, request_reeval=lambda c, p: None)


# --- 주문 안전 ------------------------------------------------------------------------------


def test_transmit_reuses_order_bound_by_sync_during_send(conn):
    """응답 대기 중 체결 동기화가 다리를 묶었으면 새 주문 행 없이, 끝난 상태도 되돌리지 않는다."""
    ctx = make_ctx(conn)
    broker = ctx.brokers[ACCT]

    def scenario(name, *args):
        if name == "place_order":
            leg = store.live_legs(conn, account_id=ACCT)[0]
            oid = repo.insert_order(conn, decision_id=None, account_id=ACCT, ticker="105W11",
                                    market="KR", side="BUY", qty=leg.qty, order_type="LIMIT",
                                    price=Decimal("350.5"), status="filled", order_no="FAKE-1",
                                    message="동기화", mode="recovered", asset="fo",
                                    origin="fo")  # fmt: skip
            store.set_leg(conn, leg.id, status="filled", order_id=oid, order_no="FAKE-1")
            conn.commit()
        return None

    broker.scenario = scenario
    executor.run_leg(ctx, account(), env(decision()))
    n = conn.execute("SELECT COUNT(*) FROM orders WHERE account_id = ?", (ACCT,)).fetchone()[0]
    assert n == 1
    assert store.legs_of(conn, store.open_positions(conn, ACCT)[0].id)[0].status == "filled"


def test_prepared_reduce_leg_is_cancelled_and_resent(conn):
    ctx = make_ctx(conn)
    pos = _held_position(conn, ctx, qty=2)
    with store.tx(conn):
        lid = store.insert_leg(conn, position_id=pos.id, account_id=ACCT, intent="CLOSE", round=0,
                               attempt=1, qty=2, price=None, order_type="MARKET")  # fmt: skip
        store.transition(conn, pos.id, expected_version=pos.version, status="closing",
                         active_reduce_leg_id=lid)  # fmt: skip
    out = executor.continue_reduce(ctx, account(), store.get_position(conn, pos.id))
    assert store.get_leg(conn, lid).status == "cancelled"
    assert out.get("status") == "accepted" and len(ctx.brokers[ACCT].orders) == 1


def test_recover_cancels_stale_prepared_entry_only(conn):
    with store.tx(conn):
        pid = store.insert_position(conn, account_id=ACCT, code="105W11", kind="FUTURES",
                                    contract_side="LONG", direction="LONG", vehicle="FUTURES",
                                    status="entering", qty_target=2)  # fmt: skip
        lid = store.insert_leg(conn, position_id=pid, account_id=ACCT, intent="OPEN", round=0,
                               attempt=1, qty=2, price=Decimal("350.5"), order_type="LIMIT")
    old = (datetime.now().astimezone() - timedelta(minutes=10)).isoformat()
    conn.execute("UPDATE fo_order_legs SET created_at = ? WHERE id = ?", (old, lid))
    conn.commit()
    positions.recover(conn, account_id=ACCT, balance=balance(), order_state_of=lambda *a: None,
                      fills_of=lambda d: [], today=TODAY, now=T0)  # fmt: skip
    assert store.get_leg(conn, lid).status == "cancelled"
    assert store.get_position(conn, pid).status == "closed"


def test_recover_defers_inflight_sending_leg(conn):
    with store.tx(conn):
        pid = store.insert_position(conn, account_id=ACCT, code="105W11", kind="FUTURES",
                                    contract_side="LONG", direction="LONG", vehicle="FUTURES",
                                    status="entering", qty_target=2)  # fmt: skip
        lid = store.insert_leg(conn, position_id=pid, account_id=ACCT, intent="OPEN", round=0,
                               attempt=1, qty=2, price=None, order_type="MARKET")
        store.set_leg(conn, lid, status="sending", sent_at=(T0 - timedelta(seconds=10)).isoformat())
    rep = positions.recover(conn, account_id=ACCT, balance=balance(),
                            order_state_of=lambda *a: None, fills_of=lambda d: [], today=TODAY,
                            now=T0)  # fmt: skip
    assert rep.deferred == [lid] and store.get_position(conn, pid).status == "entering"


def test_yesterday_accepted_leg_not_cancelled_today_and_expires(conn):
    ctx = make_ctx(conn)
    pos = _held_position(conn, ctx, qty=2, status="resizing")
    yday = (T0 - timedelta(days=1)).isoformat()
    with store.tx(conn):
        lid = store.insert_leg(conn, position_id=pos.id, account_id=ACCT, intent="RESIZE_UP",
                               round=0, attempt=1, qty=1, price=Decimal("349"),
                               order_type="LIMIT")  # fmt: skip
        store.set_leg(conn, lid, status="accepted", order_no="0007", sent_at=yday, accepted_at=yday)
    broker = ctx.brokers[ACCT]
    broker.states["0007"] = state("0007", order_qty=1, filled=0, remaining=1, terminal=False)
    rr = executor.recall_entries(ctx, account(), pos.id, reason="t")
    assert broker.cancelled == [] and rr.pending == 0
    assert store.get_leg(conn, lid).status == "expired"


def test_reduce_attempts_count_today_only(conn):
    ctx = make_ctx(conn, exit_retry_max=3)
    pos = _held_position(conn, ctx, qty=4, qty_target=4, code="205W11350", kind="CALL",
                         vehicle="OPTION_BUY")  # fmt: skip — 옵션 축소 1차는 지정가
    yday = (T0 - timedelta(days=1)).isoformat()
    with store.tx(conn):
        for a in (1, 2, 3):
            lid = store.insert_leg(conn, position_id=pos.id, account_id=ACCT, intent="RESIZE_DOWN",
                                   round=0, attempt=a, qty=1, price=None,
                                   order_type="MARKET")  # fmt: skip
            store.set_leg(conn, lid, status="filled", sent_at=yday, accepted_at=yday)
        store.transition(conn, pos.id, expected_version=store.get_position(conn, pos.id).version,
                         qty_target=3)  # fmt: skip
    out = executor.reduce(ctx, account(), store.get_position(conn, pos.id), intent="RESIZE_DOWN",
                          qty=None, reason="t")  # fmt: skip
    assert out["status"] == "accepted"
    assert ctx.brokers[ACCT].orders[-1].order_type == "LIMIT"  # 오늘 첫 시도는 지정가
    assert store.get_position(conn, pos.id).status != "exit_failed"


def test_settings_denies_fo_session():
    deny = json.loads((Path(__file__).parents[2] / ".claude" / "settings.json").read_text())[
        "permissions"]["deny"]  # fmt: skip
    assert "Bash(uv run trader fo session*)" in deny
    assert "Bash(python -m trader fo session*)" in deny


def test_settle_requires_fills_ok_and_zeroes_qty(conn):
    ctx = make_ctx(conn, market=_market(at(10, 30)), reeval={"periodic_max_per_day": 0})
    pos = _held_position(conn, ctx, qty=1, code="205W11350", kind="CALL", vehicle="OPTION_BUY",
                         last_trade_date="2026-10-26", entry_premium=Decimal("5"))  # fmt: skip
    with store.tx(conn):
        store.transition(conn, pos.id, expected_version=pos.version, status="awaiting_settlement")
    ctx.brokers[ACCT].balance = balance()
    ctx.brokers[ACCT].fail = {"get_fills"}
    _tick(ctx)
    assert store.get_position(conn, pos.id).status == "awaiting_settlement"
    ctx.brokers[ACCT].fail = set()
    _tick(ctx)
    cur = store.get_position(conn, pos.id)
    assert cur.status == "settled" and cur.qty_held == 0
    assert any("HTS" in t for t in ctx.notifier.sent)


def test_resizing_up_position_is_stop_protected(conn):
    now = at(10, 30)
    ctx = make_ctx(conn, market=_market(now, idx="340"), now=now,
                   reeval={"periodic_max_per_day": 0})  # fmt: skip
    pos = _held_position(conn, ctx, qty=2, status="resizing")
    with store.tx(conn):
        lid = store.insert_leg(conn, position_id=pos.id, account_id=ACCT, intent="RESIZE_UP",
                               round=1, attempt=1, qty=1, price=Decimal("349"),
                               order_type="LIMIT")  # fmt: skip
        store.set_leg(conn, lid, status="accepted", order_no="X9",
                      accepted_at=now.isoformat(), sent_at=now.isoformat())  # fmt: skip
    ctx.brokers[ACCT].states["X9"] = state("X9", order_qty=1, filled=0, remaining=0,
                                           cancelled=1, terminal=True, st="cancelled")
    out = _tick(ctx)
    assert out["accounts"][ACCT]["exits"][0]["intent"] == "STOP"


# --- 투자 규칙 ------------------------------------------------------------------------------


def test_resize_never_adds_contracts_to_existing_position():
    pos = g.position(kind="CALL", vehicle="OPTION_BUY", code="205W11350", qty_held=2,
                     qty_target=2, leverage_used=0.175, entry_premium=Decimal("5"),
                     premium_stop=Decimal("2.5"))  # fmt: skip
    d = decision(intent="RESIZE", vehicle="OPTION_BUY", target_leverage=0.175, order_type="MARKET",
                 entry_price=None)  # fmt: skip
    q = g.option_quote(price="3.00", delta=0.3)
    v = rules.evaluate(g.gate(d, position=pos, quote=q, underlying=Decimal("345"),
                              port=g.portfolio(positions=[pos])))  # fmt: skip
    assert not any(leg.intent == "RESIZE_UP" for leg in v.legs)


def test_hold_stop_typo_rejected_keeps_existing():
    pos = g.position(stop_pt=Decimal("345"))
    v = rules.evaluate(g.gate(decision(intent="HOLD", stop_pt=3520.0), position=pos,
                              port=g.portfolio(positions=[pos])))  # fmt: skip
    assert v.stop_pt == Decimal("345") and "거부" in v.note


def test_futures_basis_missing_is_derived_not_zero():
    from trader.fo import sizing

    q = g.quote(price="351.5", basis=None)
    assert sizing.basis_of(q, Decimal("350")) == Decimal("1.5")


def test_futures_entry_on_wrong_side_of_stop_rejected():
    # 롱 지정가 344 인데 선물 손절가 345.4(345 + 베이시스 0.5 − 슬리피지 0.1) — 체결 즉시 손절
    d = decision(entry_price=344.0)
    v = rules.evaluate(g.gate(d))
    assert not v.approved and any("반대편" in r for r in v.reasons)


def test_entry_blocked_on_day_before_forced_close():
    v = rules.evaluate(g.gate(decision(), entry_block_day=g.NOW.date()))
    assert not v.approved and any("강제 청산 직전" in r for r in v.reasons)


def test_gate_daily_loss_uses_today_change():
    port = g.portfolio(realized_today=Decimal("-4000000"), pnl_today=Decimal("0"))
    v = rules.evaluate(g.gate(decision(), port=port))
    assert not any("당일 손실 한도 초과" in r for r in v.reasons)


def test_replace_flag_only_when_close_sent_and_discarded_after_stop(conn):
    ctx = make_ctx(conn)
    pos = _held_position(conn, ctx, qty=2)
    control.set_flag(conn, "paused", True)  # 청산이 기록만 → 플래그 없음
    e = env(decision(intent="REPLACE", direction="SHORT", stop_pt=355.0, take_profit_pt=340.0),
            account_id=ACCT, position_id=pos.id, expected_version=pos.version)  # fmt: skip
    out = reeval.apply(ctx, e, round_n=1)
    assert out["replace_pending"] is False
    assert control.get_flag(conn, reeval.replace_key(ACCT, pos.id)) is None
    control.set_flag(conn, "paused", False)
    cur = store.get_position(conn, pos.id)
    e2 = e.model_copy(update={"expected_version": cur.version})
    assert reeval.apply(ctx, e2, round_n=2)["replace_pending"] is True
    # 이 라운드의 CLOSE 가 아니라 다른 경로(STOP)로 닫힌 것처럼: 다리를 STOP 으로 바꿔 체결
    leg = store.live_legs(conn, position_id=pos.id)[0]
    conn.execute("UPDATE fo_order_legs SET intent = 'STOP' WHERE id = ?", (leg.id,))
    with store.tx(conn):
        store.record_fill_progress(conn, leg.id, 2)
    executor.finalize(ctx, store.get_position(conn, pos.id))
    res = reeval.continue_replace(ctx, executor.account_of(ctx, ACCT))
    assert res and "discarded" in res[0] and len(ctx.brokers[ACCT].orders) == 1


def test_reeval_hold_tp_on_wrong_side_ignored(conn):
    ctx = make_ctx(conn)
    pos = _held_position(conn, ctx, qty=2, tp_pt=Decimal("370"))
    e = env(decision(intent="HOLD", stop_pt=346.0, take_profit_pt=340.0), account_id=ACCT,
            position_id=pos.id, expected_version=pos.version)  # fmt: skip
    out = reeval.apply(ctx, e, round_n=1)
    assert store.get_position(conn, pos.id).tp_pt == Decimal("370") and "익절선" in out["note"]


def test_position_budget_free_includes_planned():
    from trader.fo import sizing

    pos = g.position(qty_held=2, stop_fut=Decimal("345.4"), nav_at_open=Decimal("100000000"),
                     realized_loss=Decimal("100000"))  # fmt: skip
    b = sizing.position_budget(pos, ref_price=Decimal("350.5"), multiplier=50000, r=0.02)
    assert b["planned"] == Decimal("510000.0") or b["planned"] == Decimal("510000")
    assert b["free"] == b["limit"] - b["used"] - b["planned"]


# --- 재리뷰(S5R-1~6) --------------------------------------------------------------------------


def test_reduce_without_snapshot_after_balance_failure_is_blocked(conn):
    ctx = make_ctx(conn)
    pos = _held_position(conn, ctx, qty=2)
    ctx.brokers[ACCT].fail = {"get_balance"}
    out = executor.reduce(ctx, account(), pos, intent="CLOSE", qty=None, reason="t")
    assert out["status"] == "blocked" and ctx.brokers[ACCT].orders == []


def test_transmit_keeps_terminal_status_without_order_link(conn):
    ctx = make_ctx(conn)

    def scenario(name, *args):
        if name == "place_order":  # 응답 대기 중 복구가 order_id 없이 filled 로 확정
            leg = store.live_legs(conn, account_id=ACCT)[0]
            store.set_leg(conn, leg.id, status="filled", order_no="FAKE-1")
            conn.commit()
        return None

    ctx.brokers[ACCT].scenario = scenario
    executor.run_leg(ctx, account(), env(decision()))
    leg = store.legs_of(conn, store.open_positions(conn, ACCT)[0].id)[0]
    assert leg.status == "filled" and leg.order_id is not None


def test_recover_keeps_sent_day_for_nonterminal_leg(conn):
    yday = (T0 - timedelta(days=1)).isoformat()
    with store.tx(conn):
        pid = store.insert_position(conn, account_id=ACCT, code="105W11", kind="FUTURES",
                                    contract_side="LONG", direction="LONG", vehicle="FUTURES",
                                    status="entering", qty_target=2)  # fmt: skip
        lid = store.insert_leg(conn, position_id=pid, account_id=ACCT, intent="OPEN", round=0,
                               attempt=1, qty=2, price=Decimal("350.5"), order_type="LIMIT")
        store.set_leg(conn, lid, status="unknown", order_no="0042", sent_at=yday)
    positions.recover(conn, account_id=ACCT, balance=balance(),
                      order_state_of=lambda no, d: state(no, order_qty=2, filled=0, remaining=2,
                                                         terminal=False),
                      fills_of=lambda d: [], today=TODAY, now=T0)  # fmt: skip
    leg = store.get_leg(conn, lid)
    assert leg.status == "accepted" and positions.leg_day(leg) == (T0 - timedelta(days=1)).date()


def test_unstarted_session_release_not_counted_in_daily_cap(conn):
    from trader.config import HorizonProfile
    from trader.fo import triggers

    ctx = make_ctx(conn)
    pos = _held_position(conn, ctx, qty=2)
    prof = HorizonProfile(move_pct=0.04, reeval_interval_min=15, periodic_check_min=60,
                          news_min_novel=1, daily_reanalyze_every=1)  # fmt: skip
    c = ctx.settings.fo.model_copy(update={"reeval": ctx.settings.fo.reeval.model_copy(
        update={"max_per_day": 1, "interval_min": 5, "periodic_max_per_day": 4})})  # fmt: skip
    t = triggers.check(conn, position=pos, underlying=Decimal("350"), iv=None, cfg=c,
                       profile=prof, now=at(10, 0), today=TODAY,
                       ref_index=Decimal("350"))  # fmt: skip
    tid = triggers.record(conn, pos, t, now=at(10, 0))
    repo.mark_trigger_handled(conn, tid, "보류: 일시정지", handled_at=at(10, 0).isoformat())
    again = triggers.check(conn, position=pos, underlying=Decimal("350"), iv=None, cfg=c,
                           profile=prof, now=at(10, 6), today=TODAY,
                           ref_index=Decimal("350"))  # fmt: skip
    assert again is not None  # 세션이 뜨지 않은 해제는 하루 상한(1)에 세지 않는다
