"""파생 모니터 엔진(Phase 26 Task 4.2)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fo._gate import position, quote
from fo.test_executor import (
    ACCT,
    _held_position,
    balance,
    decision,
    env,
    make_ctx,
)
from trader.broker.fake import FakeMarket
from trader.broker.models import FoOrderState
from trader.config import KST
from trader.control import control
from trader.fo import engine, executor, store, triggers
from trader.ledger import repo
from trader.orchestrator import tools

DAY = date(2026, 10, 27)  # 화요일


def at(h, m=0):
    return datetime(2026, 10, 27, h, m, tzinfo=KST).astimezone(UTC)


def _market(now, *, idx="350", idx_age_s=0, fut="350.5", opt=None):
    quotes = {"105W11": quote(price=fut)}
    if opt is not None:
        quotes[opt.code] = opt
    return FakeMarket(fo_quotes=quotes,
                      kospi200=(Decimal(idx), now - timedelta(seconds=idx_age_s)))  # fmt: skip


def _ctx(conn, now, market=None, **fo):
    fo.setdefault("reeval", {"periodic_max_per_day": 0})
    return make_ctx(conn, now=now, market=market or _market(now), **fo)


def _tick(ctx, calls=None):
    sink = calls if calls is not None else []
    return engine.tick(ctx, request_reeval=lambda c, p: sink.append(p))


def _orders(ctx):
    return [(o.side, o.qty) for o in ctx.brokers[ACCT].orders]


def test_monitor_runs_from_0845_futures_stop_only(conn):
    now = at(8, 50)
    ctx = _ctx(conn, now, _market(now, idx="340"))  # 지수는 손절선 아래지만 09:00 전이라 무시
    _held_position(conn, ctx, qty=2, stop_fut=Decimal("346"))
    _tick(ctx)
    assert _orders(ctx) == []
    ctx.market_client = _market(now, idx="340", fut="345.5")
    out = _tick(ctx)
    assert _orders(ctx) == [("SELL", 2)]
    assert out["accounts"][ACCT]["exits"][0]["intent"] == "STOP"


def test_index_stale_uses_futures_only(conn):
    now = at(10, 30)
    ctx = _ctx(conn, now, _market(now, idx="344", idx_age_s=600))
    _held_position(conn, ctx, qty=2, stop_fut=Decimal("340"))
    _tick(ctx)
    assert _orders(ctx) == []
    ctx.market_client = _market(now, idx="344")
    _tick(ctx)
    assert _orders(ctx) == [("SELL", 2)]


def _short_call(conn, ctx, *, premium_stop):
    opt = quote(code="205W11350", kind="CALL", price="2.1", delta=0.5, iv=18.0,
                strike=Decimal("350"), basis=None)  # fmt: skip
    pos = _held_position(conn, ctx, qty=2, code="205W11350", kind="CALL", contract_side="SHORT",
                         direction="SHORT", vehicle="OPTION_SELL", stop_pt=Decimal("360"),
                         entry_premium=Decimal("1.5"), approved_mult=2.0,
                         premium_stop=premium_stop)  # fmt: skip
    return opt, pos


def test_option_stop_uses_stored_premium_stop_not_ratio(conn):
    now = at(10, 30)
    ctx = _ctx(conn, now)
    opt, pos = _short_call(conn, ctx, premium_stop=Decimal("2.0"))
    ctx.market_client = _market(now, opt=opt.model_copy(update={"price": Decimal("1.9")}))
    _tick(ctx)
    assert _orders(ctx) == []
    ctx.market_client = _market(now, opt=opt)  # 2.1 ≥ 저장선 2.0 (평균가 × 배수 3.0 이 아님)
    _tick(ctx)
    assert _orders(ctx) == [("BUY", 2)]


def test_maintain_leverage_breach_reduces(conn):
    now = at(10, 30)
    ctx = _ctx(conn, now, max_contracts=20)
    pos = _held_position(conn, ctx, qty=12, qty_target=12)  # 12 × 1,750만 = 2.1억 > 2배
    out = _tick(ctx)
    assert _orders(ctx) == [("SELL", 1)]
    assert out["accounts"][ACCT]["maintain"][0]["target"] == 11
    assert store.get_position(conn, pos.id).qty_target == 11


def test_maintain_orders_option_sell_first(conn):
    ctx = _ctx(conn, at(10, 30))
    fut = position(id=1, qty_held=1)
    sell = position(id=2, code="205W11350", kind="CALL", contract_side="SHORT",
                    direction="SHORT", vehicle="OPTION_SELL", qty_held=1)  # fmt: skip
    bal = balance(add_margin=Decimal("1"))
    acts, _ = engine.maintain(ctx, executor.account_of(ctx, ACCT), [fut, sell], bal=bal,
                              quotes={}, index=Decimal("350"), now=at(10, 30),
                              today=DAY)  # fmt: skip
    assert acts[0][0].id == 2 and acts[0][1] == 0


def test_maintain_stress_breach_at_1500(conn):
    now = at(14, 30)
    ctx = _ctx(conn, now, hold_overnight=True, overnight_risk_pct=0.001)
    _held_position(conn, ctx, qty=2)
    _tick(ctx)
    assert _orders(ctx) == []
    assert any("유지 조건" in m for m in ctx.notifier.sent)
    ctx.now = lambda: at(15, 0)
    ctx.market_client = _market(at(15, 0))
    _tick(ctx)
    assert _orders(ctx) == [("SELL", 1)]


def _open_with_live_entry(conn, ctx):
    out = executor.run_leg(ctx, executor.account_of(ctx, ACCT), env())
    assert out[0]["status"] == "accepted"
    leg = store.live_legs(conn, account_id=ACCT)[0]
    ctx.brokers[ACCT].states[leg.order_no] = FoOrderState(
        order_no=leg.order_no, order_qty=leg.qty, filled_qty=0, remaining_qty=0,
        rejected_qty=0, cancelled_qty=leg.qty, terminal=True, state="cancelled",
    )  # fmt: skip
    return leg


def test_halted_recalls_entries(conn):
    now = at(10, 30)
    ctx = _ctx(conn, now)
    _open_with_live_entry(conn, ctx)
    b = ctx.brokers[ACCT].balance
    ctx.brokers[ACCT].balance = b.model_copy(
        update={"cash": b.cash.model_copy(update={"realized_today": Decimal("-4000000")})}
    )
    out = _tick(ctx)
    acc = out["accounts"][ACCT]
    assert acc["halted"] is True and acc["recalled"][0]["cancelled"] == 1
    assert control.get_flag(conn, executor.halted_key(ACCT)) == DAY.isoformat()
    assert any("일일 손실" in m for m in ctx.notifier.sent)


def test_enabled_false_recalls_entries(conn):
    now = at(10, 30)
    ctx = _ctx(conn, now)
    _open_with_live_entry(conn, ctx)
    ctx.settings = ctx.settings.model_copy(
        update={"fo": ctx.settings.fo.model_copy(update={"enabled": False})}
    )
    out = _tick(ctx)
    assert out["accounts"][ACCT]["recalled"][0]["reason"] == "fo.enabled=false"


def test_force_close_day_0900(conn):
    ctx = _ctx(conn, at(8, 55), roll_days=2)
    _held_position(conn, ctx, qty=2, last_trade_date="2026-10-29")  # 목요일 → D-2 = 화요일
    _tick(ctx)
    assert _orders(ctx) == []
    ctx.now = lambda: at(9, 0)
    ctx.market_client = _market(at(9, 0))
    out = _tick(ctx)
    assert _orders(ctx) == [("SELL", 2)]
    assert out["accounts"][ACCT]["exits"][0]["intent"] == "EXPIRY"


def test_hold_overnight_false_daily_close(conn):
    ctx = _ctx(conn, at(15, 30))
    _held_position(conn, ctx, qty=2)
    _tick(ctx)
    assert _orders(ctx) == []
    ctx.now = lambda: at(15, 35)
    ctx.market_client = _market(at(15, 35))
    out = _tick(ctx)
    assert out["accounts"][ACCT]["exits"][0]["intent"] == "SESSION_CLOSE"
    assert _orders(ctx) == [("SELL", 2)]


def test_hold_overnight_true_no_daily_close(conn):
    ctx = _ctx(conn, at(15, 40), hold_overnight=True)
    _held_position(conn, ctx, qty=2)
    _tick(ctx)
    assert _orders(ctx) == []


def test_expiry_forced_close_discards_pending_reeval(conn):
    ctx = _ctx(conn, at(9, 10), roll_days=2)
    pos = _held_position(conn, ctx, qty=2, last_trade_date="2026-10-29")
    tid = triggers.record(conn, pos, triggers.Trigger("news", {}, None), now=at(9, 5))
    _tick(ctx)
    row = conn.execute("SELECT handled_at, session_id FROM triggers WHERE id = ?",
                       (tid,)).fetchone()  # fmt: skip
    assert row["handled_at"] and row["session_id"].startswith("폐기")


def _pending_flag(conn, today=DAY):
    control.set_flag(conn, f"{tools.FO_PENDING_FLAG}{ACCT}", {
        "date": today.isoformat(), "stage": "_fo_decision",
        "envelope": env(decision()).model_dump(mode="json"), "request_id": "r1",
        "requested_at": at(8, 25).isoformat(), "dry_run": False,
    })  # fmt: skip


def test_pending_entry_runs_in_window_and_discards_after_cutoff(conn):
    ctx = _ctx(conn, at(8, 55))
    _pending_flag(conn)
    _tick(ctx)
    assert _orders(ctx) == []  # 진입 창(09:00) 전에는 기다린다
    assert control.get_flag(conn, f"{tools.FO_PENDING_FLAG}{ACCT}") is not None
    ctx.now = lambda: at(9, 5)
    ctx.brokers[ACCT]._now = lambda: at(9, 5)  # 잔고 요청 시각도 09:05(실행 직전 재조회)
    ctx.market_client = _market(at(9, 5))
    out = _tick(ctx)
    res = out["accounts"][ACCT]["entries"][0]
    assert res["pending"] == "r1" and res["results"][0]["status"] == "accepted", res
    assert [o.side for o in ctx.brokers[ACCT].orders] == ["BUY"]
    assert control.get_flag(conn, f"{tools.FO_PENDING_FLAG}{ACCT}") is None


def test_pending_entry_cutoff_discard(conn):
    ctx = _ctx(conn, at(15, 32))
    _pending_flag(conn)
    out = _tick(ctx)
    assert "cutoff" in out["accounts"][ACCT]["entries"][0]["discarded"]
    assert _orders(ctx) == []
    assert control.get_flag(conn, f"{tools.FO_PENDING_FLAG}{ACCT}") is None


def test_snapshot_written_without_positions(conn):
    ctx = _ctx(conn, at(10, 30))
    out = _tick(ctx)
    assert out["accounts"][ACCT]["snapshot"] is True
    n = conn.execute("SELECT COUNT(*) FROM fo_nav_snapshot WHERE account_id = ?",
                     (ACCT,)).fetchone()[0]  # fmt: skip
    assert n == 1


def test_trigger_records_and_requests_reeval(conn):
    ctx = _ctx(conn, at(10, 30), reeval={"periodic_max_per_day": 4})
    pos = _held_position(conn, ctx, qty=2)
    calls: list[dict] = []
    out = _tick(ctx, calls)
    t = out["accounts"][ACCT]["triggers"][0]
    assert t["kind"] == "periodic" and calls and calls[0]["position_id"] == pos.id
    assert calls[0]["trigger_id"] == t["trigger_id"] and calls[0]["round"] == 1
    assert repo.get_trigger(conn, t["trigger_id"])["kind"] == "fo:periodic"
    _tick(ctx, calls)
    assert len(calls) == 1  # 미처리 트리거가 있으면 새로 내지 않는다


def test_awaiting_settlement_settles_when_gone(conn):
    ctx = _ctx(conn, at(10, 30))
    pos = _held_position(conn, ctx, qty=2, last_trade_date="2026-10-26")
    with store.tx(conn):
        store.transition(conn, pos.id, expected_version=pos.version, status="awaiting_settlement")
    ctx.brokers[ACCT].balance = balance()
    _tick(ctx)
    assert store.get_position(conn, pos.id).status == "settled"


def test_exit_reason_pure_cases():
    cfg = make_ctx.__globals__["settings"]().fo
    pos = position(stop_pt=Decimal("345"), tp_pt=Decimal("360"))
    assert engine.exit_reason(pos, cfg=cfg, index=Decimal("361"), quote=None, now=at(10, 0),
                              today=DAY)[0] == "CLOSE"  # fmt: skip
    assert engine.exit_reason(pos, cfg=cfg, index=None, quote=None, now=at(10, 0),
                              today=DAY) is None  # fmt: skip
    short = position(direction="SHORT", contract_side="SHORT", stop_pt=Decimal("355"))
    assert engine.exit_reason(short, cfg=cfg, index=Decimal("356"), quote=None, now=at(10, 0),
                              today=DAY)[0] == "STOP"  # fmt: skip
