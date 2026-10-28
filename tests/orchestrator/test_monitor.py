from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from helpers_agents import ok_decision

from trader.agents.schemas import Decision
from trader.broker.fake import FakeBroker, FakeMarket
from trader.broker.models import Cash, Position
from trader.config import AccountConfig, Settings
from trader.control import control
from trader.ledger import repo
from trader.orchestrator import monitor
from trader.orchestrator.run_store import read_checkpoint, run_dir, write_checkpoint

D = date(2026, 10, 9)
NOW = datetime(2026, 10, 9, 2, 0, tzinfo=UTC)


class Sent:
    def __init__(self):
        self.msgs = []

    def send(self, text, *, escape=True):
        self.msgs.append(text)
        return True


def _pos(ticker="005930", avg="70000", cur="63000", qty=10):
    return Position(
        ticker=ticker,
        market="KR",
        qty=qty,
        avg_price=Decimal(avg),
        current_price=Decimal(cur),
        eval_amount=Decimal(cur) * qty,
        pnl_pct=float(Decimal(cur) / Decimal(avg) - 1),
    )


# --- 순수 함수 ------------------------------------------------------------------------


def test_check_position_stop_loss_from_decision_and_default():
    pos = _pos(cur="64000")
    dec = ok_decision(stop_loss=65000.0)
    sig = monitor.check_position(pos, dec, Decimal("64000"), stop_loss_pct=-0.15, now=NOW)
    assert [s.kind for s in sig] == ["stop_loss"] and sig[0].price == Decimal("64000")
    # 결정에 손절가가 없으면 평단 -15%
    sig = monitor.check_position(pos, None, Decimal("59000"), stop_loss_pct=-0.15, now=NOW)
    assert [s.kind for s in sig] == ["stop_loss"] and sig[0].detail["level"] == "59500.00"
    # 손절선 위지만 평단 대비 -14%: 급등락 트리거
    kinds = [
        s.kind
        for s in monitor.check_position(pos, None, Decimal("60000"), stop_loss_pct=-0.15, now=NOW)
    ]
    assert kinds == ["move_7pct"]


def test_parse_price_conditions():
    conds = ["20일선 이탈", "종가 < 65000", "70,000원 이상 돌파 시", "RSI > 80"]
    assert monitor.parse_price_conditions(conds) == [
        ("<", Decimal("65000")),
        (">=", Decimal("70000")),
    ]


def test_take_profit_move_and_invalidation_signals():
    pos = _pos(avg="70000", cur="80000")
    dec = ok_decision(take_profit=80000.0, invalidation_conditions=["종가 < 60000"])
    kinds = [
        s.kind
        for s in monitor.check_position(pos, dec, Decimal("80000"), stop_loss_pct=-0.15, now=NOW)
    ]
    assert kinds == ["take_profit", "move_7pct"]  # +14% 급등도 함께
    kinds = [
        s.kind
        for s in monitor.check_position(pos, dec, Decimal("74000"), stop_loss_pct=-0.15, now=NOW)
    ]
    assert kinds == []  # +5.7%: 아무 신호 없음
    dec2 = ok_decision(stop_loss=None, invalidation_conditions=["종가 < 66000"])
    kinds = [
        s.kind
        for s in monitor.check_position(pos, dec2, Decimal("65500"), stop_loss_pct=-0.15, now=NOW)
    ]
    assert kinds == ["invalidation"]  # -6.4% 는 급등락 아님


# --- monitor_once (I/O) ----------------------------------------------------------------


def _accounts(paper_mode="auto"):
    return [
        AccountConfig(id="paper-main", env="paper", cano="1", markets=["KR"], mode=paper_mode),
        AccountConfig(id="real-main", env="real", cano="2", markets=["KR"], mode="off"),
    ]


def _ctx(conn, *, positions, price="63000", paper_mode="auto", runner=None):
    accounts = _accounts(paper_mode)
    cash = {"KR": Cash(currency="KRW", available=Decimal("10000000"), total=Decimal("10000000"))}
    brokers = {a.id: FakeBroker(a, positions=positions, cash=cash) for a in accounts}
    calls = []

    def session_runner(spec, settings, conn_, accounts_, **kw):
        from trader.orchestrator.session import SessionResult

        calls.append(spec)
        return SessionResult(ok=True, session_id=f"s{len(calls)}", fake=True)

    ctx = monitor.MonitorContext(
        settings=Settings(),
        accounts=accounts,
        conn=conn,
        brokers=brokers,
        market_client=FakeMarket(prices={"005930": Decimal(price)}),
        notifier=Sent(),
        now=lambda: NOW,
        session_runner=runner or session_runner,
    )
    ctx.calls = calls  # type: ignore[attr-defined]
    return ctx


def _decision(conn, **over):
    dec = Decision(**ok_decision(**over))
    d = run_dir(D, "KR", "005930")
    write_checkpoint(
        d, "30_decision", {"schema_version": 1, "data": dec.model_dump(), "call": None}
    )
    repo.ingest_checkpoint(conn, D, "KR", "005930", "30_decision", {"data": dec.model_dump()})
    return dec


def test_stop_loss_sells_immediately_even_in_approve_mode(conn):
    _decision(conn, stop_loss=65000.0)
    ctx = _ctx(conn, positions=[_pos(cur="63000")], price="63000", paper_mode="approve")
    control.set_account_mode(conn, ctx.accounts[0], "approve")
    r = monitor.monitor_once(ctx, market="KR", today=D)
    assert r["stop_loss_orders"] == 1 and r["checked"] == 2
    orders = ctx.brokers["paper-main"].orders
    assert len(orders) == 1 and orders[0].side == "SELL" and orders[0].qty == 10
    assert repo.pending_approvals(conn) == []  # 승인 없이 즉시
    assert any("손절" in m for m in ctx.notifier.msgs)
    assert (
        read_checkpoint(run_dir(D, "KR", "005930"), "55_order_sl_paper-main")["data"]["status"]
        == "accepted"
    )
    rc = read_checkpoint(run_dir(D, "KR", "005930"), "45_stop_loss_paper-main")["data"]
    assert rc["approved"] is True and rc["side"] == "SELL"


def test_stop_loss_off_account_records_only(conn):
    _decision(conn, stop_loss=65000.0)
    ctx = _ctx(conn, positions=[_pos(cur="63000")], price="63000")
    monitor.monitor_once(ctx, market="KR", today=D)
    assert ctx.brokers["real-main"].orders == []
    assert (
        read_checkpoint(run_dir(D, "KR", "005930"), "45_stop_loss_real-main")["data"]["mode"]
        == "off"
    )


def test_stop_loss_not_repeated_same_day(conn):
    _decision(conn, stop_loss=65000.0)
    ctx = _ctx(conn, positions=[_pos(cur="63000")], price="63000")
    monitor.monitor_once(ctx, market="KR", today=D)
    monitor.monitor_once(ctx, market="KR", today=D)
    assert len(ctx.brokers["paper-main"].orders) == 1  # 같은 결정·계좌·SELL 중복 방지


def test_trigger_reevaluation_rate_limited(conn):
    _decision(conn, take_profit=80000.0)
    ctx = _ctx(conn, positions=[_pos(avg="70000", cur="80000")], price="80000")
    r1 = monitor.monitor_once(ctx, market="KR", today=D)
    assert r1["triggers"] >= 1 and r1["reevaluated"] == 1 and len(ctx.calls) == 1
    assert ctx.calls[0].kind == "resume" and ctx.calls[0].trigger["kind"] == "take_profit"
    ctx.now = lambda: NOW + timedelta(minutes=10)
    r2 = monitor.monitor_once(ctx, market="KR", today=D)
    assert (
        r2["triggers"] == 0 and r2["reevaluated"] == 0 and len(ctx.calls) == 1
    )  # 미처리 행 재사용
    ctx.now = lambda: NOW + timedelta(minutes=31)
    r3 = monitor.monitor_once(ctx, market="KR", today=D)
    # 같은 운영일에 같은 종류(take_profit)로 이미 재평가했으면 조건이 지속돼도 다시 띄우지 않는다
    # (지속 조건이 30분마다 유료 세션을 재생성해 일일 상한을 소진하던 문제)
    assert r3["reevaluated"] == 0 and r3["triggers"] == 0 and len(ctx.calls) == 1
    rows = conn.execute("SELECT kind, handled_at FROM triggers ORDER BY id").fetchall()
    # 같은 틱의 트리거(take_profit + move_7pct + 단기 프로필의 periodic)는 세션 1회가 모두 처리
    assert {r["kind"] for r in rows} == {"take_profit", "move_7pct", "periodic"}
    assert all(r["handled_at"] is not None for r in rows)
    # 다음 운영일에는 (그날 결정이 있으면) 다시 1회
    dec = Decision(**ok_decision(take_profit=80000.0))
    repo.ingest_checkpoint(
        conn, D + timedelta(days=1), "KR", "005930", "30_decision", {"data": dec.model_dump()}
    )
    ctx.now = lambda: NOW + timedelta(days=1)
    r5 = monitor.monitor_once(ctx, market="KR", today=D + timedelta(days=1))
    assert r5["reevaluated"] == 1 and len(ctx.calls) == 2


def test_failed_reevaluation_can_retry_same_day(conn):
    """세션 기동 실패로 '실패:' 로 처리된 트리거는 하루 1회 제한에서 빼서 30분 뒤 재시도한다."""
    _decision(conn, take_profit=80000.0)
    calls = []

    def runner(spec, *a, **kw):
        calls.append(spec)
        if len(calls) == 1:
            raise RuntimeError("claude 없음")
        from trader.orchestrator.session import SessionResult

        return SessionResult(ok=True, session_id="s2", fake=True)

    ctx = _ctx(conn, positions=[_pos(avg="70000", cur="80000")], price="80000", runner=runner)
    r1 = monitor.monitor_once(ctx, market="KR", today=D)
    assert r1["reevaluated"] == 0 and len(calls) == 1
    ctx.now = lambda: NOW + timedelta(minutes=31)
    r2 = monitor.monitor_once(ctx, market="KR", today=D)
    assert r2["reevaluated"] == 1 and len(calls) == 2


def test_reevaluation_blocked_when_paused_or_cap(conn):
    _decision(conn, take_profit=80000.0)
    ctx = _ctx(conn, positions=[_pos(avg="70000", cur="80000")], price="80000")
    control.set_flag(conn, "paused", True)
    r = monitor.monitor_once(ctx, market="KR", today=D)
    assert r["reevaluated"] == 0 and r["triggers"] >= 1 and ctx.calls == []
    control.set_flag(conn, "paused", False)
    ctx.settings = Settings(llm={"daily_session_cap": 0})
    ctx.now = lambda: NOW + timedelta(hours=1)
    r = monitor.monitor_once(ctx, market="KR", today=D)
    assert r["reevaluated"] == 0 and ctx.calls == []


def test_monitor_without_positions_checks_nothing(conn):
    ctx = _ctx(conn, positions=[])
    r = monitor.monitor_once(ctx, market="KR", today=D)
    assert r == {"checked": 0, "stop_loss_orders": 0, "triggers": 0, "reevaluated": 0}


def test_quote_failure_skips_ticker(conn):
    _decision(conn, stop_loss=65000.0)
    ctx = _ctx(conn, positions=[_pos(cur="63000")])
    ctx.market_client = FakeMarket(prices={}, fail={"get_price_kr"})
    r = monitor.monitor_once(ctx, market="KR", today=D)
    assert r["stop_loss_orders"] == 0 and ctx.brokers["paper-main"].orders == []


@pytest.mark.parametrize("cur", ["63000"])
def test_stop_loss_dry_run_records_without_order(conn, cur):
    _decision(conn, stop_loss=65000.0)
    ctx = _ctx(conn, positions=[_pos(cur=cur)], price=cur)
    ctx.dry_run = True
    r = monitor.monitor_once(ctx, market="KR", today=D)
    assert r["stop_loss_orders"] == 0 and ctx.brokers["paper-main"].orders == []
    assert (
        read_checkpoint(run_dir(D, "KR", "005930"), "45_stop_loss_paper-main")["data"]["dry_run"]
        is True
    )


# --- 리뷰 반영 ---------------------------------------------------------------------


def _old_decision(conn, **over):
    """어제 날짜의 결정 (보유 중인 종목의 일반적인 상태)."""
    dec = Decision(**ok_decision(**over))
    yesterday = D - timedelta(days=1)
    d = run_dir(yesterday, "KR", "005930")
    write_checkpoint(
        d, "30_decision", {"schema_version": 1, "data": dec.model_dump(), "call": None}
    )
    repo.ingest_checkpoint(
        conn, yesterday, "KR", "005930", "30_decision", {"data": dec.model_dump()}
    )
    return dec


def test_stop_loss_dedupes_when_decision_is_old(conn):
    _old_decision(conn, stop_loss=65000.0)
    ctx = _ctx(conn, positions=[_pos(cur="63000")], price="63000")
    monitor.monitor_once(ctx, market="KR", today=D)
    monitor.monitor_once(ctx, market="KR", today=D)
    assert len(ctx.brokers["paper-main"].orders) == 1
    assert conn.execute("SELECT COUNT(*) FROM risk_checks").fetchone()[0] >= 1
    # 손절 체크포인트는 오늘 분석의 40_/50_ 를 덮어쓰지 않는 별도 이름
    d = run_dir(D, "KR", "005930")
    assert read_checkpoint(d, "45_stop_loss_paper-main")["data"]["side"] == "SELL"
    assert read_checkpoint(d, "55_order_sl_paper-main")["data"]["status"] == "accepted"
    assert read_checkpoint(d, "40_risk_check_paper-main") is None


def test_reevaluation_failure_does_not_block_other_positions(conn):
    _decision(conn, take_profit=80000.0)
    dec_b = Decision(**ok_decision(ticker="000660", stop_loss=90000.0))
    repo.ingest_checkpoint(conn, D, "KR", "000660", "30_decision", {"data": dec_b.model_dump()})
    pos_a = _pos("005930", avg="70000", cur="80000")
    pos_b = _pos("000660", avg="100000", cur="85000")

    def boom(spec, settings, conn_, accounts_, **kw):
        raise RuntimeError("claude 없음")

    ctx = _ctx(conn, positions=[pos_a, pos_b], price="80000", runner=boom)
    ctx.market_client = FakeMarket(prices={"005930": Decimal("80000"), "000660": Decimal("85000")})
    r = monitor.monitor_once(ctx, market="KR", today=D)
    assert r["stop_loss_orders"] == 1  # B 손절은 A 재평가 실패와 무관하게 나간다
    assert any(o.ticker == "000660" for o in ctx.brokers["paper-main"].orders)
    row = conn.execute(
        "SELECT handled_at, session_id FROM triggers WHERE ticker='005930'"
    ).fetchone()
    assert row["handled_at"] is not None and "실패" in (row["session_id"] or "")


def test_zero_quote_skipped(conn):
    _decision(conn, stop_loss=65000.0)
    ctx = _ctx(conn, positions=[_pos(cur="63000")], price="0")
    r = monitor.monitor_once(ctx, market="KR", today=D)
    assert r["stop_loss_orders"] == 0 and ctx.brokers["paper-main"].orders == []


def test_us_stop_loss_uses_marketable_limit():
    dec = monitor._stop_loss_decision(
        "AAPL", "US", Decimal("200"), monitor.Signal("stop_loss", Decimal("200"), {})
    )
    assert dec.order_type == "LIMIT" and dec.limit_price == 194.0  # 3% 아래 지정가
    dec_kr = monitor._stop_loss_decision(
        "005930", "KR", Decimal("63000"), monitor.Signal("stop_loss", Decimal("63000"), {})
    )
    assert dec_kr.order_type == "MARKET"


def test_paused_stop_loss_notifies_once(conn):
    _decision(conn, stop_loss=65000.0)
    ctx = _ctx(conn, positions=[_pos(cur="63000")], price="63000")
    control.set_flag(conn, "paused", True)
    monitor.monitor_once(ctx, market="KR", today=D)
    monitor.monitor_once(ctx, market="KR", today=D)
    msgs = [m for m in ctx.notifier.msgs if "일시정지" in m and "손절" in m]
    assert len(msgs) == 1 and ctx.brokers["paper-main"].orders == []


def test_trigger_not_duplicated_while_unhandled(conn):
    _decision(conn, take_profit=80000.0)
    ctx = _ctx(conn, positions=[_pos(avg="70000", cur="80000")], price="80000")
    control.set_flag(conn, "paused", True)  # 재평가는 막히고 기록만
    monitor.monitor_once(ctx, market="KR", today=D)
    monitor.monitor_once(ctx, market="KR", today=D)
    n = conn.execute("SELECT COUNT(*) FROM triggers WHERE kind='take_profit'").fetchone()[0]
    assert n == 1  # 미처리 트리거가 있으면 같은 종류를 다시 쌓지 않는다


def test_stop_loss_bookkeeping_failure_does_not_abort_tick_or_repeat(conn, monkeypatch):
    """접수 뒤 원장 기록 실패(OrderBookkeepingError)가 나머지 손절 점검을 끊거나
    다음 틱에 같은 SELL 을 다시 내게 하면 안 된다."""
    from trader.orchestrator import executor as executor_mod

    _decision(conn, stop_loss=65000.0)
    dec_b = Decision(**ok_decision(ticker="000660", stop_loss=90000.0))
    repo.ingest_checkpoint(conn, D, "KR", "000660", "30_decision", {"data": dec_b.model_dump()})
    positions = [_pos("005930", cur="63000"), _pos("000660", avg="100000", cur="85000")]
    ctx = _ctx(conn, positions=positions)
    ctx.market_client = FakeMarket(prices={"005930": Decimal("63000"), "000660": Decimal("85000")})

    def boom(*a, **kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(repo, "insert_order", boom)
    r = monitor.monitor_once(ctx, market="KR", today=D)
    orders = ctx.brokers["paper-main"].orders
    assert {o.ticker for o in orders} == {"005930", "000660"}  # 둘 다 점검됐다
    assert r["stop_loss_orders"] == 0  # 기록이 실패한 주문은 성공으로 세지 않는다
    monitor.monitor_once(ctx, market="KR", today=D)
    assert len(ctx.brokers["paper-main"].orders) == 2  # 접수된 주문을 다시 내지 않는다
    assert executor_mod.OrderBookkeepingError is not None


def test_filled_trim_earlier_today_does_not_suppress_stop_loss(conn):
    """오전에 체결된 TRIM(SELL, filled)이 잔량의 손절을 하루 동안 막으면 안 된다."""
    dec = _decision(conn, stop_loss=65000.0)
    dec_id = repo.decision_id(conn, D, "KR", "005930")
    repo.insert_order(
        conn,
        decision_id=dec_id,
        account_id="paper-main",
        ticker=dec.ticker,
        market="KR",
        side="SELL",
        qty=5,
        order_type="MARKET",
        price=None,
        status="filled",
        order_no="T1",
        message="TRIM",
        mode="auto",
        created_at=(NOW - timedelta(hours=1)).isoformat(),
    )
    ctx = _ctx(conn, positions=[_pos(cur="63000", qty=5)], price="63000")
    r = monitor.monitor_once(ctx, market="KR", today=D)
    assert r["stop_loss_orders"] == 1 and len(ctx.brokers["paper-main"].orders) == 1


def test_stale_accepted_sell_of_old_decision_does_not_block_stop_loss(conn):
    """어제 미체결로 끝난 손절 SELL(상태 accepted 그대로)이 오늘 손절을 영구히 막으면 안 된다."""
    _old_decision(conn, stop_loss=65000.0)
    dec_id = repo.decision_id(conn, D - timedelta(days=1), "KR", "005930")
    repo.insert_order(
        conn,
        decision_id=dec_id,
        account_id="paper-main",
        ticker="005930",
        market="KR",
        side="SELL",
        qty=10,
        order_type="MARKET",
        price=None,
        status="accepted",
        order_no="OLD",
        message="손절",
        mode="auto",
        created_at=(NOW - timedelta(days=1)).isoformat(),
    )
    ctx = _ctx(conn, positions=[_pos(cur="63000")], price="63000")
    r = monitor.monitor_once(ctx, market="KR", today=D)
    assert r["stop_loss_orders"] == 1 and len(ctx.brokers["paper-main"].orders) == 1


def test_failed_session_result_does_not_lock_trigger_kind_for_day(conn):
    """세션이 떴지만 실패(ok=False: 타임아웃·예산)한 재평가도 '실패:' 로 표시해 하루 1회 제한에서
    빼야 한다. uuid 로 handled 표시되면 그 종류는 그날 다시 재평가되지 않는다."""
    from trader.orchestrator.session import SessionResult

    _decision(conn, take_profit=80000.0)
    calls = []

    def runner(spec, *a, **kw):
        calls.append(spec)
        if len(calls) == 1:
            return SessionResult(ok=False, session_id="uuid-1", error="timeout", fake=True)
        return SessionResult(ok=True, session_id="uuid-2", fake=True)

    ctx = _ctx(conn, positions=[_pos(avg="70000", cur="80000")], price="80000", runner=runner)
    r1 = monitor.monitor_once(ctx, market="KR", today=D)
    assert r1["reevaluated"] == 0 and len(calls) == 1
    sql = "SELECT session_id FROM triggers WHERE handled_at IS NOT NULL"
    handled = conn.execute(sql).fetchall()
    assert handled and all(r["session_id"].startswith("실패:") for r in handled)
    ctx.now = lambda: NOW + timedelta(minutes=31)
    r2 = monitor.monitor_once(ctx, market="KR", today=D)
    assert r2["reevaluated"] == 1 and len(calls) == 2


def test_no_decision_today_skips_session_and_keeps_trigger_pending(conn):
    """오늘 30_decision 이 없으면(analyze 미완료·실패) 유료 세션을 띄워도 submit 이 거부되므로
    세션 없이 트리거만 미처리로 남겨 다음 틱에 다시 본다."""
    _old_decision(conn, take_profit=80000.0)
    ctx = _ctx(conn, positions=[_pos(avg="70000", cur="80000")], price="80000")
    r = monitor.monitor_once(ctx, market="KR", today=D)
    assert r["triggers"] >= 1 and r["reevaluated"] == 0 and ctx.calls == []
    rows = conn.execute("SELECT handled_at FROM triggers").fetchall()
    assert rows and all(r["handled_at"] is None for r in rows)


def test_us_position_exchange_from_broker_used_for_quote_and_sell(conn):
    """universe.us 에 없는 US 보유분(수동 매수·관심종목 해제)도 브로커가 준 거래소로 시세를 읽고
    손절 매도를 낸다 (ctx.exchanges 에 없으면 NAS 로 조회해 시세 0 → 손절 불능이었다)."""
    acct = AccountConfig(id="paper-main", env="paper", cano="1", markets=["US"], mode="auto")
    pos = Position(
        ticker="XOM",
        market="US",
        qty=3,
        avg_price=Decimal("120"),
        current_price=Decimal("100"),
        eval_amount=Decimal("300"),
        pnl_pct=-0.17,
        exchange="NYS",
    )
    cash = {"US": Cash(currency="USD", available=Decimal("10000"), total=Decimal("10000"))}
    seen = []

    class Market(FakeMarket):
        def get_price_us(self, symbol, exchange):
            seen.append(exchange)
            return super().get_price_us(symbol, exchange)

    ctx = monitor.MonitorContext(
        settings=Settings(),
        accounts=[acct],
        conn=conn,
        brokers={acct.id: FakeBroker(acct, positions=[pos], cash=cash)},
        market_client=Market(prices={"XOM": Decimal("100")}),
        notifier=Sent(),
        now=lambda: NOW,
    )
    r = monitor.monitor_once(ctx, market="US", today=D)
    assert seen == ["NYS"] and r["stop_loss_orders"] == 1
    order = ctx.brokers["paper-main"].orders[0]
    assert order.exchange == "NYS" and order.side == "SELL"


def test_parse_price_conditions_ignores_volume_and_picks_price_number():
    conds = [
        "거래량 10,000,000 미만",  # 거래량은 가격 조건이 아니다
        "시총 5000억 하회",
        "3일 연속 종가 65000 이하",  # 첫 숫자(3)가 아니라 가격(65000)
        "RSI 80 초과 과열",
    ]
    assert monitor.parse_price_conditions(conds) == [("<", Decimal("65000"))]


def test_stop_loss_refreshes_fill_status_before_dedupe(conn):
    """원장 status 는 fills sync(마감 뒤)만 바꾸므로 오전에 체결된 TRIM 이 장중엔 accepted 로 남아
    손절을 막는다 → 막힐 때 그 계좌의 체결을 한 번 동기화해 filled 면 손절을 낸다."""
    from trader.broker.models import Fill

    dec = _decision(conn, stop_loss=65000.0)
    dec_id = repo.decision_id(conn, D, "KR", "005930")
    repo.insert_order(
        conn,
        decision_id=dec_id,
        account_id="paper-main",
        ticker=dec.ticker,
        market="KR",
        side="SELL",
        qty=5,
        order_type="MARKET",
        price=None,
        status="accepted",  # 체결됐지만 원장은 아직 모른다
        order_no="T1",
        message="TRIM",
        mode="auto",
        created_at=(NOW - timedelta(hours=1)).isoformat(),
    )
    ctx = _ctx(conn, positions=[_pos(cur="63000", qty=5)], price="63000")
    fill = Fill(
        order_no="T1",
        ticker="005930",
        market="KR",
        side="SELL",
        qty=5,
        price=Decimal("70500"),
        filled_at=NOW - timedelta(minutes=50),
    )
    ctx.brokers["paper-main"].fills = {("KR", D): [fill]}
    r = monitor.monitor_once(ctx, market="KR", today=D)
    assert r["stop_loss_orders"] == 1 and len(ctx.brokers["paper-main"].orders) == 1
    st = conn.execute("SELECT status FROM orders WHERE order_no='T1'").fetchone()["status"]
    assert st == "filled"


def test_stop_loss_cancels_resting_sell_then_sells(conn):
    """아침 TRIM 지정가가 미체결로 남아 있으면 손절은 그 주문을 취소하고 매도한다(이전엔 하루 종일
    막혔다)."""
    pos = _pos()
    ctx = _ctx(conn, positions=[pos])
    _decision(conn, action="HOLD", stop_loss=65000.0)
    did = repo.decision_id(conn, D, "KR", "005930")
    repo.insert_order(
        conn,
        decision_id=did,
        account_id="paper-main",
        ticker="005930",
        market="KR",
        side="SELL",
        qty=3,
        order_type="LIMIT",
        price=Decimal("72000"),
        status="accepted",
        order_no="T1",
        message=None,
        mode="auto",
        org_no="91252",
        created_at=NOW.isoformat(),
    )
    out = monitor.monitor_once(ctx, market="KR", today=D)
    broker = ctx.brokers["paper-main"]
    assert broker.cancelled == ["T1"]
    assert (
        conn.execute("SELECT status FROM orders WHERE order_no='T1'").fetchone()[0] == "cancelled"
    )
    assert out["stop_loss_orders"] == 1
    sells = [o for o in broker.orders if o.side == "SELL"]
    assert len(sells) == 1 and sells[0].qty == 10


def test_stop_loss_stays_blocked_when_cancel_fails(conn, monkeypatch):
    pos = _pos()
    ctx = _ctx(conn, positions=[pos])
    _decision(conn, action="HOLD", stop_loss=65000.0)
    did = repo.decision_id(conn, D, "KR", "005930")
    repo.insert_order(
        conn,
        decision_id=did,
        account_id="paper-main",
        ticker="005930",
        market="KR",
        side="SELL",
        qty=3,
        order_type="LIMIT",
        price=Decimal("72000"),
        status="accepted",
        order_no="T2",
        message=None,
        mode="auto",
        created_at=NOW.isoformat(),
    )
    from trader.broker.errors import KisApiError

    def boom(*a, **kw):
        raise KisApiError("X", "취소 실패", "T")

    monkeypatch.setattr(ctx.brokers["paper-main"], "cancel_order", boom)
    out = monitor.monitor_once(ctx, market="KR", today=D)
    assert out["stop_loss_orders"] == 0
    assert any("손절 보류" in m for m in ctx.notifier.msgs)


def test_reevaluation_is_enqueued_when_daemon_provides_queue(conn):
    """데몬에서는 재평가 세션을 틱 안에서 돌리지 않고 큐로 넘긴다(손절 점검이 멈추지 않도록)."""
    pos = _pos(cur="76000")  # +7% 넘김 → 트리거
    ctx = _ctx(conn, positions=[pos], price="76000")
    _decision(conn, action="HOLD", stop_loss=60000.0, take_profit=None)
    queued = []
    ctx.enqueue_reevaluate = lambda params: queued.append(params) or 7
    out = monitor.monitor_once(ctx, market="KR", today=D)
    assert out["triggers"] >= 1 and out.get("queued") == 1 and ctx.calls == []
    assert queued[0]["ticker"] == "005930" and queued[0]["trigger_id"] >= 1


# --- 리뷰 F13·F16 ----------------------------------------------------------------------


def test_dry_run_stop_loss_is_recorded_once_per_day(conn):
    """dry-run 손절은 기록만 하므로 매 분 45_/risk_checks 를 다시 쓰지 않는다."""
    _decision(conn, stop_loss=65000.0)
    ctx = _ctx(conn, positions=[_pos(cur="63000")], price="63000")
    ctx.dry_run = True
    monitor.monitor_once(ctx, market="KR", today=D)
    n1 = conn.execute("SELECT COUNT(*) AS n FROM risk_checks").fetchone()["n"]
    assert n1 >= 1 and ctx.brokers["paper-main"].orders == []
    monitor.monitor_once(ctx, market="KR", today=D)
    assert conn.execute("SELECT COUNT(*) AS n FROM risk_checks").fetchone()["n"] == n1


def test_old_unhandled_trigger_is_not_reused_today(conn):
    """어제 처리 못 한 트리거는 오늘 다시 쓰지 않고 오늘 가격으로 새 트리거를 쌓는다."""
    _decision(conn, stop_loss=50000.0, take_profit=75000.0)
    repo.insert_trigger(
        conn,
        market="KR",
        ticker="005930",
        kind="take_profit",
        detail={},
        price=Decimal("76000"),
        decision_id=None,
        account_id="paper-main",
        created_at="2026-10-08T02:00:00+00:00",
    )
    assert repo.unhandled_trigger_id(conn, "KR", "005930", "take_profit") == 1
    assert repo.unhandled_trigger_id(conn, "KR", "005930", "take_profit", D) is None
    ctx = _ctx(conn, positions=[_pos(avg="70000", cur="80000")], price="80000")
    monitor.monitor_once(ctx, market="KR", today=D)
    rows = conn.execute(
        "SELECT id, created_at FROM triggers WHERE kind='take_profit' ORDER BY id"
    ).fetchall()
    assert len(rows) == 2 and rows[1]["created_at"].startswith("2026-10-09")


# --- 보유기간 프로필(Phase 23 T) ------------------------------------------------------


def test_check_position_move_threshold_follows_profile():
    pos = _pos(avg="70000", cur="73000")
    # mid(7%) 기본: +4.3% 는 신호 없음, 단기 임계 4% 를 넘기면 move_7pct(임계 표기 포함)
    assert monitor.check_position(pos, None, Decimal("73000"), stop_loss_pct=-0.15, now=NOW) == []
    sig = monitor.check_position(
        pos, None, Decimal("73000"), stop_loss_pct=-0.15, now=NOW, move_pct=Decimal("0.04")
    )
    assert [s.kind for s in sig] == ["move_7pct"] and sig[0].detail["threshold"] == "4.00%"
    # 장기 임계 10%: +9% 도 조용
    assert (
        monitor.check_position(
            pos, None, Decimal("76300"), stop_loss_pct=-0.15, now=NOW, move_pct=Decimal("0.10")
        )
        == []
    )


def test_periodic_signal_rules():
    from trader.risk import horizon

    prof = Settings().risk.horizon_profiles["short"]
    kw = dict(profile=prof, last_periodic_at=None, count_today=0, now=NOW)
    # 변동 2%(= move_pct/2) 이상이어야 한다
    assert monitor.periodic_signal(Decimal("70700"), Decimal("70000"), **kw) is None
    sig = monitor.periodic_signal(Decimal("71500"), Decimal("70000"), **kw)
    assert sig is not None and sig.kind == "periodic" and sig.detail["ref_price"] == "70000"
    # 간격(60분) 안이면 없음, 지나면 다시
    kw2 = kw | {"last_periodic_at": NOW - timedelta(minutes=30)}
    assert monitor.periodic_signal(Decimal("71500"), Decimal("70000"), **kw2) is None
    kw3 = kw | {"last_periodic_at": NOW - timedelta(minutes=61)}
    assert monitor.periodic_signal(Decimal("71500"), Decimal("70000"), **kw3) is not None
    # 운영일 상한, mid/long 프로필(periodic_check_min=0)은 없음
    assert (
        monitor.periodic_signal(
            Decimal("71500"), Decimal("70000"), **(kw | {"count_today": horizon.PERIODIC_DAILY_CAP})
        )
        is None
    )
    mid = Settings().risk.horizon_profiles["mid"]
    assert (
        monitor.periodic_signal(Decimal("80000"), Decimal("70000"), **(kw | {"profile": mid}))
        is None
    )


def test_long_horizon_holding_uses_wider_threshold_and_longer_interval(conn):
    _decision(conn, horizon_days=60, take_profit=None, invalidation_conditions=[])
    ctx = _ctx(conn, positions=[_pos(avg="70000", cur="76000")], price="76000")
    r1 = monitor.monitor_once(ctx, market="KR", today=D)  # +8.6%: 장기 임계 10% 미달
    assert r1["triggers"] == 0 and len(ctx.calls) == 0
    ctx.market_client.prices["005930"] = Decimal("77500")  # +10.7%
    r2 = monitor.monitor_once(ctx, market="KR", today=D)
    assert r2["triggers"] == 1 and r2["reevaluated"] == 1
    # 장기 간격 60분: 45분 뒤 다른 종류(무효화)가 와도 재평가는 보류된다
    conn.execute(
        "UPDATE decisions SET json=json_set(json, '$.invalidation_conditions', json(?))",
        ('["종가 < 60000"]',),
    )
    ctx.market_client.prices["005930"] = Decimal("59000")
    ctx.now = lambda: NOW + timedelta(minutes=45)
    r3 = monitor.monitor_once(ctx, market="KR", today=D)
    assert r3["reevaluated"] == 0 and len(ctx.calls) == 1


def test_stop_loss_does_not_cancel_resting_sell_when_paused_or_off(conn):
    """전체 검토 O5: 일시정지·off 계좌에서는 손절 전 미체결 SELL 을 KIS 에서 취소하지 않는다
    (취소만 하고 손절은 안 나가면 보호 매도만 사라진다)."""
    from trader.control import control

    pos = _pos()
    ctx = _ctx(conn, positions=[pos])
    _decision(conn, action="HOLD", stop_loss=65000.0)
    did = repo.decision_id(conn, D, "KR", "005930")
    repo.insert_order(
        conn,
        decision_id=did,
        account_id="paper-main",
        ticker="005930",
        market="KR",
        side="SELL",
        qty=3,
        order_type="LIMIT",
        price=Decimal("72000"),
        status="accepted",
        order_no="T1",
        message=None,
        mode="auto",
        org_no="91252",
        created_at=NOW.isoformat(),
    )
    control.set_flag(conn, "paused", True)
    monitor.monitor_once(ctx, market="KR", today=D)
    assert ctx.brokers["paper-main"].cancelled == []
    control.set_flag(conn, "paused", False)
    control.set_account_mode(conn, ctx.accounts[0], "off")
    monitor.monitor_once(ctx, market="KR", today=D)
    assert ctx.brokers["paper-main"].cancelled == []
    assert conn.execute("SELECT status FROM orders WHERE order_no='T1'").fetchone()[0] == "accepted"


def test_off_mode_stop_record_does_not_block_after_switch_to_auto(conn):
    """전체 검토 O15: off 의 "오늘 기록함" 플래그가 같은 날 auto 전환 뒤 손절을 막지 않는다."""
    from trader.control import control

    pos = _pos()
    ctx = _ctx(conn, positions=[pos])
    _decision(conn, action="HOLD", stop_loss=65000.0)
    control.set_account_mode(conn, ctx.accounts[0], "off")
    monitor.monitor_once(ctx, market="KR", today=D)
    assert ctx.brokers["paper-main"].orders == []
    control.set_account_mode(conn, ctx.accounts[0], "auto")
    out = monitor.monitor_once(ctx, market="KR", today=D)
    assert out["stop_loss_orders"] == 1


def test_stop_loss_cancels_same_day_resting_buy(conn):
    """전체 검토 O13: 손절 때 같은 종목 당일 미체결 BUY 를 취소해 손절 뒤 재보유를 막는다."""
    pos = _pos()
    ctx = _ctx(conn, positions=[pos])
    _decision(conn, action="ADD", stop_loss=65000.0)
    did = repo.decision_id(conn, D, "KR", "005930")
    repo.insert_order(
        conn,
        decision_id=did,
        account_id="paper-main",
        ticker="005930",
        market="KR",
        side="BUY",
        qty=2,
        order_type="LIMIT",
        price=Decimal("60000"),
        status="accepted",
        order_no="B1",
        message=None,
        mode="auto",
        org_no="91252",
        created_at=NOW.isoformat(),
    )
    out = monitor.monitor_once(ctx, market="KR", today=D)
    assert out["stop_loss_orders"] == 1
    assert ctx.brokers["paper-main"].cancelled == ["B1"]
    assert (
        conn.execute("SELECT status FROM orders WHERE order_no='B1'").fetchone()[0] == "cancelled"
    )


def test_stop_loss_expires_pending_buy_approval(conn):
    """손절 직후 같은 종목의 pending BUY 승인을 만료로 닫는다(사람이 승인해 곧바로 되사지 않게)."""
    import json

    pos = _pos()
    ctx = _ctx(conn, positions=[pos])
    _decision(conn, action="ADD", stop_loss=65000.0)
    did = repo.decision_id(conn, D, "KR", "005930")
    conn.execute(
        "INSERT INTO approvals(decision_id, account_id, order_json, status, requested_at, "
        "expires_at) VALUES (?,?,?,?,?,?)",
        (
            did,
            "paper-main",
            json.dumps({"ticker": "005930", "side": "BUY"}),
            "pending",
            NOW.isoformat(),
            NOW.isoformat(),
        ),
    )
    conn.commit()
    monitor.monitor_once(ctx, market="KR", today=D)
    assert conn.execute("SELECT status FROM approvals").fetchone()[0] == "expired"


def test_stop_loss_is_monotone_across_holding_decisions(conn):
    """전체 검토 L5: 보유 재분석 HOLD 가 손절가를 비워도 보유 기간 중 이전 손절선(최고값)을 지킨다.
    (평단 70000·현재 63000: 기본 −15% 손절선 59500 은 안 닿지만 이전 결정의 65000 은 닿는다)"""
    from datetime import timedelta

    prev = D - timedelta(days=1)
    first = Decision(**ok_decision(stop_loss=65000.0))
    did = repo.ingest_checkpoint(
        conn, prev, "KR", "005930", "30_decision", {"data": first.model_dump()}
    )
    repo.insert_order(
        conn,
        decision_id=did,
        account_id="paper-main",
        ticker="005930",
        market="KR",
        side="BUY",
        qty=10,
        order_type="LIMIT",
        price=Decimal("70000"),
        status="filled",
        order_no="F1",
        message=None,
        mode="auto",
        created_at=(NOW - timedelta(days=1)).isoformat(),
    )
    _decision(conn, action="HOLD", target_weight=0.0, stop_loss=None)  # 오늘 HOLD, 손절가 없음
    ctx = _ctx(conn, positions=[_pos(cur="63000")], price="63000")
    out = monitor.monitor_once(ctx, market="KR", today=D)
    assert out["stop_loss_orders"] == 1


def test_stop_loss_floor_survives_same_day_reevaluation(conn):
    """전체 검토 2차 2-O1: 매수 당일 BUY(손절 65000) 뒤 오후 재평가 `31_decision_r1` HOLD 가
    손절가를 비워도(같은 (date, market, ticker) 행을 덮어쓴다) 아침 손절선이 살아 있어야 한다.
    held_since 가 오늘이라 이전 날짜 행이 없다 — 평단 70000·현재 63000: 기본 −15% 는 안 닿는다."""
    first = Decision(**ok_decision(stop_loss=65000.0))
    did = repo.ingest_checkpoint(
        conn, D, "KR", "005930", "30_decision", {"data": first.model_dump()}
    )
    repo.insert_order(
        conn,
        decision_id=did,
        account_id="paper-main",
        ticker="005930",
        market="KR",
        side="BUY",
        qty=10,
        order_type="LIMIT",
        price=Decimal("70000"),
        status="filled",
        order_no="F1",
        message=None,
        mode="auto",
        created_at=NOW.isoformat(),
    )
    later = Decision(**ok_decision(action="HOLD", target_weight=0.0, stop_loss=None))
    repo.ingest_checkpoint(conn, D, "KR", "005930", "31_decision_r1", {"data": later.model_dump()})
    assert repo.get_decision(conn, did)["action"] == "HOLD"  # 행은 덮였다
    ctx = _ctx(conn, positions=[_pos(cur="63000")], price="63000")
    out = monitor.monitor_once(ctx, market="KR", today=D)
    assert out["stop_loss_orders"] == 1
