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
        read_checkpoint(run_dir(D, "KR", "005930"), "50_order_paper-main")["data"]["status"]
        == "accepted"
    )
    rc = read_checkpoint(run_dir(D, "KR", "005930"), "40_risk_check_paper-main")["data"]
    assert rc["approved"] is True and rc["side"] == "SELL"


def test_stop_loss_off_account_records_only(conn):
    _decision(conn, stop_loss=65000.0)
    ctx = _ctx(conn, positions=[_pos(cur="63000")], price="63000")
    monitor.monitor_once(ctx, market="KR", today=D)
    assert ctx.brokers["real-main"].orders == []
    assert (
        read_checkpoint(run_dir(D, "KR", "005930"), "40_risk_check_real-main")["data"]["mode"]
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
    assert r2["triggers"] >= 1 and r2["reevaluated"] == 0 and len(ctx.calls) == 1
    ctx.now = lambda: NOW + timedelta(minutes=31)
    r3 = monitor.monitor_once(ctx, market="KR", today=D)
    assert r3["reevaluated"] == 1 and len(ctx.calls) == 2
    rows = conn.execute("SELECT kind, handled_at FROM triggers ORDER BY id").fetchall()
    assert rows[0]["handled_at"] is not None and len(rows) >= 3


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
        read_checkpoint(run_dir(D, "KR", "005930"), "40_risk_check_paper-main")["data"]["dry_run"]
        is True
    )
