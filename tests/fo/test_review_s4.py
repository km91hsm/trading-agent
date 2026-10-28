"""Phase 26 단계 4 교차 리뷰 반영(Codex exec X·codex review R·Claude C)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fo._gate import quote
from fo.test_engine import _market, _open_with_live_entry, _pending_flag, at
from fo.test_executor import ACCT, _held_position, balance, decision, env, holding, make_ctx
from trader.broker.fake import FakeMarket
from trader.control import control
from trader.fo import engine, positions, reeval, store, triggers
from trader.ledger import repo
from trader.orchestrator import tools

DAY = date(2026, 10, 27)


def _ctx(conn, now, **kw):
    kw.setdefault("reeval", {"periodic_max_per_day": 0})
    market = kw.pop("market", None) or _market(now)
    return make_ctx(conn, now=now, market=market, **kw)


def _tick(ctx, calls=None, ret=None):
    sink = calls if calls is not None else []

    def req(c, p):
        sink.append(p)
        return ret

    return engine.tick(ctx, request_reeval=req)


# --- C1·X7: 기록만 모드에서 상태를 소비·변경하지 않는다 ---------------------------------------


def test_dry_run_tick_keeps_pending_flag_and_records_no_trigger(conn):
    ctx = _ctx(conn, at(9, 5), dry_run=True, reeval={"periodic_max_per_day": 4})
    _pending_flag(conn)
    _held_position(conn, ctx, qty=2)
    calls: list = []
    out = _tick(ctx, calls)
    assert out["accounts"][ACCT]["entries"][0]["held"] == "dry_run"
    assert control.get_flag(conn, f"{tools.FO_PENDING_FLAG}{ACCT}") is not None
    assert calls == [] and conn.execute("SELECT COUNT(*) FROM triggers").fetchone()[0] == 0
    assert ctx.brokers[ACCT].orders == []


def test_paused_risk_reduce_does_not_lower_target(conn):
    ctx = _ctx(conn, at(10, 30), max_contracts=20)
    pos = _held_position(conn, ctx, qty=12, qty_target=12)
    control.set_flag(conn, "paused", True)
    out = _tick(ctx)
    m = out["accounts"][ACCT]["maintain"][0]
    assert m["status"] == "recorded" and store.get_position(conn, pos.id).qty_target == 12
    assert ctx.brokers[ACCT].orders == []


def test_reeval_hold_tp_unchanged_when_paused(conn):
    ctx = _ctx(conn, at(10, 30))
    pos = _held_position(conn, ctx, qty=2, tp_pt=Decimal("370"))
    control.set_flag(conn, "paused", True)
    e = env(decision(intent="HOLD", stop_pt=347.0, take_profit_pt=351.0), account_id=ACCT,
            position_id=pos.id, expected_version=pos.version)  # fmt: skip
    reeval.apply(ctx, e, round_n=1)
    assert store.get_position(conn, pos.id).tp_pt == Decimal("370")


# --- C3: 일일 손실은 오늘 변화분 -----------------------------------------------------------


def _cash(ctx, unrealized):
    b = ctx.brokers[ACCT].balance
    ctx.brokers[ACCT].balance = b.model_copy(
        update={"cash": b.cash.model_copy(update={"unrealized": Decimal(unrealized)})}
    )


def test_daily_loss_uses_today_change_not_cumulative(conn):
    ctx = _ctx(conn, at(9, 0))
    _cash(ctx, "-4000000")  # 며칠 전부터의 평가손 −4%(NAV 1억)
    assert _tick(ctx)["accounts"][ACCT]["halted"] is False
    _cash(ctx, "-6500000")  # 오늘 −2.5%: 한도(3%) 안
    assert _tick(ctx)["accounts"][ACCT]["halted"] is False
    _cash(ctx, "-7500000")  # 오늘 −3.5%
    assert _tick(ctx)["accounts"][ACCT]["halted"] is True


# --- X4: 진입 중 미체결 예약도 유지 조건에 --------------------------------------------------


def test_maintain_counts_pending_entry_and_recalls_first(conn):
    ctx = _ctx(conn, at(10, 30), max_contracts=20)
    leg = _open_with_live_entry(conn, ctx)  # OPEN 다리 접수(미체결)
    pos = store.get_position(conn, leg.position_id)
    with store.tx(conn):  # 일부 체결: 보유 1 + 미체결(다리 수량 − 1)
        store.record_fill_progress(conn, leg.id, 1)
    ctx.brokers[ACCT].balance = balance([holding(qty=1)], nav=Decimal("20000000"))
    out = _tick(ctx)
    m = out["accounts"][ACCT]["maintain"]
    assert m and m[0]["status"] == "recalled", out["accounts"][ACCT]
    assert store.get_leg(conn, leg.id).status == "cancelled"
    assert store.get_position(conn, pos.id).qty_held == 1


# --- X5: 운영 경로 휴장일 ------------------------------------------------------------------


def test_force_close_day_uses_market_holidays(conn):
    now = at(9, 0)
    market = FakeMarket(fo_quotes={"105W11": quote()}, kospi200=(Decimal("350"), now),
                        holidays={date(2026, 10, 29)})  # fmt: skip
    ctx = _ctx(conn, now, roll_days=2, market=market)
    _held_position(conn, ctx, qty=2, last_trade_date="2026-10-30")  # 금 D, 목 휴장 → 화 청산
    out = _tick(ctx)
    assert out["accounts"][ACCT]["exits"][0]["intent"] == "EXPIRY"


# --- R4·X9·C6: 트리거 빈도의 계좌 범위·폐기 비계산·등록 실패 -----------------------------------


def test_trigger_throttle_is_per_account(conn):
    cfg = make_ctx(conn).settings.fo
    from trader.config import HorizonProfile

    prof = HorizonProfile(move_pct=0.04, reeval_interval_min=15, periodic_check_min=60,
                          news_min_novel=1, daily_reanalyze_every=1)  # fmt: skip
    from fo._gate import position

    a = position(id=1, account_id="paper-fo")
    b = position(id=2, account_id="paper-fo2")
    t = triggers.check(conn, position=a, underlying=Decimal("365"), iv=None, cfg=cfg,
                       profile=prof, now=at(10, 0), today=DAY,
                       ref_index=Decimal("350"))  # fmt: skip
    triggers.record(conn, a, t, now=at(10, 0))  # A 는 대기 중
    tb = triggers.check(conn, position=b, underlying=Decimal("365"), iv=None, cfg=cfg,
                        profile=prof, now=at(10, 1), today=DAY,
                        ref_index=Decimal("350"))  # fmt: skip
    assert tb is not None and tb.kind == "move"


def test_discarded_trigger_not_counted(conn):
    ctx = _ctx(conn, at(10, 0))
    pos = _held_position(conn, ctx, qty=2)
    from trader.config import HorizonProfile

    prof = HorizonProfile(move_pct=0.04, reeval_interval_min=15, periodic_check_min=60,
                          news_min_novel=1, daily_reanalyze_every=1)  # fmt: skip
    cfg = ctx.settings.fo
    t = triggers.check(conn, position=pos, underlying=Decimal("365"), iv=None, cfg=cfg,
                       profile=prof, now=at(10, 0), today=DAY,
                       ref_index=Decimal("350"))  # fmt: skip
    tid = triggers.record(conn, pos, t, now=at(10, 0))
    repo.mark_trigger_handled(conn, tid, "폐기: 포지션 변경", handled_at=at(10, 1).isoformat())
    again = triggers.check(conn, position=pos, underlying=Decimal("365"), iv=None, cfg=cfg,
                           profile=prof, now=at(10, 2), today=DAY,
                           ref_index=Decimal("350"))  # fmt: skip
    assert again is not None and again.kind == "move"


def test_enqueue_failure_marks_trigger_failed(conn):
    ctx = _ctx(conn, at(10, 30), reeval={"periodic_max_per_day": 4})
    _held_position(conn, ctx, qty=2)
    out = _tick(ctx, ret=False)
    tid = out["accounts"][ACCT]["triggers"][0]["trigger_id"]
    row = conn.execute("SELECT session_id, handled_at FROM triggers WHERE id = ?",
                       (tid,)).fetchone()  # fmt: skip
    assert row["handled_at"] and row["session_id"].startswith("실패")


# --- C7: 뉴스 트리거는 종목 태그 없는 뉴스만 --------------------------------------------------


def test_novel_news_counts_untagged_only(conn):
    now = datetime(2026, 10, 27, 1, 30, tzinfo=UTC)
    for i, tags in enumerate(('["005930"]', "[]")):
        conn.execute(
            "INSERT INTO news_items(market, source, title, url, url_hash, received_at, "
            "tickers_json, novel) VALUES ('KR', 's', ?, ?, ?, ?, ?, 1)",
            (f"t{i}", f"u{i}", f"h{i}", (now - timedelta(minutes=5)).isoformat(), tags),
        )
    conn.commit()
    assert engine._novel_news(conn, now, 30) == 1


# --- X6: adopt 은 소유 계좌만 ----------------------------------------------------------------


def test_adopt_refuses_other_account(conn):
    with store.tx(conn):
        pid = store.insert_position(conn, account_id="paper-fo2", code="105W11", kind="FUTURES",
                                    contract_side="LONG", direction="LONG", vehicle="FUTURES",
                                    status="unknown", qty_held=1)  # fmt: skip
    bal = balance([holding(qty=1)])
    assert positions.adopt(conn, position_id=pid, side="LONG", kind="FUTURES", qty=1,
                           stop_pt=Decimal("345"), balance=bal,
                           account_id="paper-fo") is False  # fmt: skip
    assert positions.adopt(conn, position_id=pid, side="LONG", kind="FUTURES", qty=1,
                           stop_pt=Decimal("345"), balance=bal,
                           account_id="paper-fo2") is True  # fmt: skip
