"""Phase 25 단계 7: 주간 리밸런스 — 밴드 밖만, TRIM 먼저, 결정 행 없음, 게이트 그대로."""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from conftest import make_account

from trader.broker.fake import FakeBroker
from trader.broker.models import Cash, Position
from trader.config import Settings
from trader.control import control
from trader.orchestrator import executor, rebalance
from trader.orchestrator.run_store import read_checkpoint, run_dir

D = date(2026, 10, 30)  # 금요일
NOW = datetime(2026, 10, 30, 5, 30, tzinfo=UTC)


def _pos(t, qty, px="100000"):
    p = Decimal(px)
    return Position(
        ticker=t,
        market="KR",
        qty=qty,
        avg_price=p,
        current_price=p,
        eval_amount=p * qty,
        pnl_pct=0.0,
        sellable_qty=qty,
    )


class P:
    def __init__(self, ev):
        self.eval_amount = Decimal(ev)


def test_only_beyond_band():
    legs = rebalance.drift_legs(
        {"A": P("300"), "B": P("120")}, Decimal("1000"), {"A": 0.2, "B": 0.1}, band=0.05
    )
    assert [(x.ticker, x.action) for x in legs] == [("A", "TRIM")]  # B 는 +2%p 라 밴드 안


def test_trim_before_add():
    legs = rebalance.drift_legs(
        {"A": P("100"), "B": P("400"), "C": P("320")},
        Decimal("1000"),
        {"A": 0.25, "B": 0.25, "C": 0.2},
        band=0.05,
    )
    assert [(x.ticker, x.action) for x in legs] == [("B", "TRIM"), ("C", "TRIM"), ("A", "ADD")]


def test_excludes_hedge_and_scalp():
    legs = rebalance.drift_legs(
        {"A": P("400"), "H": P("400")}, Decimal("1000"), {"A": 0.1, "H": 0.1}, exclude={"H"}
    )
    assert [x.ticker for x in legs] == ["A"]


def _decision(conn, ticker, action, weight, d="2026-10-27", source="llm"):
    data = {
        "ticker": ticker,
        "action": action,
        "target_weight": weight,
        "order_type": "LIMIT",
        "limit_price": 100000,
        "stop_loss": 90000,
        "take_profit": 120000,
        "horizon_days": 20,
        "confidence": 0.7,
        "rationale": "r",
        "invalidation_conditions": [],
    }
    conn.execute(
        "INSERT INTO decisions(date, market, ticker, action, target_weight, json, created_at, "
        "source) VALUES (?,?,?,?,?,?,?,?)",
        (d, "KR", ticker, action, weight, json.dumps(data), f"{d}T00:00:00+00:00", source),
    )
    conn.commit()


def test_hold_decision_has_no_target(conn):
    _decision(conn, "005930", "HOLD", 0.0)
    assert rebalance.target_decision(conn, "KR", "005930", D, 30) is None
    _decision(conn, "000660", "BUY", 0.1, d="2026-08-01")
    assert rebalance.target_decision(conn, "KR", "000660", D, 30) is None  # 너무 오래됨
    _decision(conn, "035420", "BUY", 0.1)
    assert rebalance.target_decision(conn, "KR", "035420", D, 30)["target_weight"] == 0.1


@pytest.fixture
def ctx(conn, project):
    acct = make_account("paper-main", "paper", "auto", ["KR"])
    cash = {
        "KR": Cash(
            currency="KRW",
            available=Decimal("5000000"),
            total=Decimal("5000000"),
            nav=Decimal("10000000"),
        )
    }
    # NAV 1천만: 005930 30%(목표 15%) → TRIM, 000660 2%(목표 10%) → ADD
    broker = FakeBroker(acct, positions=[_pos("005930", 30), _pos("000660", 2)], cash=cash)
    return executor.ExecContext(
        settings=Settings(), accounts=[acct], conn=conn, brokers={acct.id: broker}, now=lambda: NOW
    )


def _seed(conn):
    _decision(conn, "005930", "BUY", 0.15)
    _decision(conn, "000660", "BUY", 0.10)
    # 최소 보유 3거래일을 넘기도록 오래전 매수 체결
    for t in ("005930", "000660"):
        conn.execute(
            "INSERT INTO orders(account_id, ticker, market, side, qty, order_type, status, mode, "
            "created_at) VALUES ('paper-main',?,'KR','BUY',1,'LIMIT','filled','auto',?)",
            (t, (NOW - timedelta(days=20)).isoformat()),
        )
    conn.commit()


def test_rebalance_stage_names_and_null_decision(ctx, conn):
    _seed(conn)
    out = rebalance.run(
        ctx, market="KR", today=D, dry_run=False, quote_fn=lambda t: Decimal("100000")
    )
    assert [(o["ticker"], o["action"]) for o in out["orders"]] == [
        ("005930", "TRIM"),
        ("000660", "ADD"),
    ]
    b = ctx.brokers["paper-main"]
    assert [(o.ticker, o.side) for o in b.orders] == [("005930", "SELL"), ("000660", "BUY")]
    assert b.orders[0].qty == 15  # 30% → 15%
    d = run_dir(D, "KR", "005930")
    assert read_checkpoint(d, "43_rebalance_paper-main")["data"]["approved"] is True
    assert read_checkpoint(d, "53_order_rebalance_paper-main")["data"]["status"] == "accepted"
    assert read_checkpoint(d, "40_risk_check_paper-main") is None
    rows = conn.execute(
        "SELECT decision_id FROM orders WHERE created_at >= ?", (NOW.isoformat(),)
    ).fetchall()
    assert rows and all(r["decision_id"] is None for r in rows)


def test_rebalance_runs_once_per_day(ctx, conn):
    _seed(conn)
    rebalance.run(ctx, market="KR", today=D, dry_run=False, quote_fn=lambda t: Decimal("100000"))
    assert rebalance.run(
        ctx, market="KR", today=D, dry_run=False, quote_fn=lambda t: Decimal("100000")
    ) == {"skipped": "오늘 이미 실행"}


def test_rebalance_skips_open_order_today(ctx, conn):
    _seed(conn)
    conn.execute(
        "INSERT INTO orders(account_id, ticker, market, side, qty, order_type, status, mode, "
        "created_at) VALUES ('paper-main','005930','KR','SELL',1,'LIMIT','accepted','auto',?)",
        (NOW.isoformat(),),
    )
    conn.commit()
    out = rebalance.run(
        ctx, market="KR", today=D, dry_run=False, quote_fn=lambda t: Decimal("100000")
    )
    assert any(leg.get("skipped") for leg in out["legs"] if leg["ticker"] == "005930")
    assert [o.ticker for o in ctx.brokers["paper-main"].orders] == ["000660"]


def test_rebalance_respects_gate_rejections(ctx, conn):
    _seed(conn)
    conn.execute(
        "UPDATE orders SET created_at=? WHERE side='BUY'", ((NOW - timedelta(hours=1)).isoformat(),)
    )
    conn.commit()  # 오늘 매수 → TRIM 은 최소 보유 미충족, ADD 는 당일 중복 매수로 게이트 거부
    out = rebalance.run(
        ctx, market="KR", today=D, dry_run=False, quote_fn=lambda t: Decimal("100000")
    )
    rs = {o["ticker"]: o["results"][0] for o in out["orders"]}
    assert rs["005930"]["approved"] is False and any(
        "최소 보유" in r for r in rs["005930"]["reasons"]
    )
    assert ctx.brokers["paper-main"].orders == []


def test_rebalance_dry_run_no_orders(ctx, conn):
    _seed(conn)
    out = rebalance.run(
        ctx, market="KR", today=D, dry_run=True, quote_fn=lambda t: Decimal("100000")
    )
    assert out["orders"] and ctx.brokers["paper-main"].orders == []
    assert control.get_flag(conn, rebalance.done_key("KR", D))["dry_run"] is True


def test_disabled_skips(ctx, conn):
    ctx.settings = Settings(rebalance={"enabled": False})
    assert rebalance.run(ctx, market="KR", today=D, dry_run=True, quote_fn=lambda t: 1)["skipped"]


def test_daemon_registers_friday_rebalance_cron():
    from test_daemon import SCHED

    from trader import daemon

    jobs = {j.id: j for j in daemon.build_jobs(SCHED, Settings(), markets=["KR", "US"])}
    assert jobs["kr_rebalance"].cron == {
        "hour": 14,
        "minute": 30,
        "day_of_week": "fri",
        "timezone": "Asia/Seoul",
    }
    assert jobs["us_rebalance"].cron["timezone"] == SCHED.us.tz  # 그 시장 스케줄의 tz
    off = daemon.build_jobs(SCHED, Settings(rebalance={"enabled": False}), markets=["KR"])
    assert not any(j.kind == "rebalance" for j in off)


def test_rebalance_skips_pending_approval(ctx, conn):
    """approve 계좌에 같은 감축의 pending 승인이 있으면 리밸런스가 두 번째 요청을 만들지 않는다."""
    from trader.ledger import repo

    _seed(conn)
    did = conn.execute("SELECT id FROM decisions WHERE ticker='005930'").fetchone()[0]
    repo.create_approval(
        conn,
        decision_id=did,
        account_id="paper-main",
        order={"ticker": "005930", "market": "KR", "side": "SELL", "qty": 15},
        requested_at=(NOW - timedelta(minutes=5)).isoformat(),
        expires_at=(NOW + timedelta(minutes=25)).isoformat(),
    )
    out = rebalance.run(
        ctx, market="KR", today=D, dry_run=False, quote_fn=lambda t: Decimal("100000")
    )
    assert any(leg.get("skipped") for leg in out["legs"] if leg["ticker"] == "005930")
    assert [o.ticker for o in ctx.brokers["paper-main"].orders] == ["000660"]


def test_rebalance_add_passes_change_pct_to_chase_gate(ctx, conn):
    """시세의 당일 등락률이 게이트로 가서 급등 종목 ADD 는 추격 금지로 거부된다."""
    from types import SimpleNamespace

    _seed(conn)
    q = SimpleNamespace(price=Decimal("100000"), change_pct=0.12)
    out = rebalance.run(ctx, market="KR", today=D, dry_run=False, quote_fn=lambda t: q)
    rs = {o["ticker"]: o["results"][0] for o in out["orders"]}
    assert rs["000660"]["approved"] is False
    assert any("급등" in r for r in rs["000660"]["reasons"])
    assert [o.side for o in ctx.brokers["paper-main"].orders] == ["SELL"]  # TRIM 은 면제


def test_rebalance_keeps_zero_confidence(ctx, conn, monkeypatch):
    _seed(conn)
    conn.execute(
        "UPDATE decisions SET json=json_set(json, '$.confidence', 0.0) WHERE ticker='000660'"
    )
    conn.commit()
    seen = []
    real = executor.execute

    def spy(ctx_, dec, **kw):
        seen.append((dec.ticker, dec.confidence))
        return real(ctx_, dec, **kw)

    monkeypatch.setattr(executor, "execute", spy)
    rebalance.run(ctx, market="KR", today=D, dry_run=True, quote_fn=lambda t: Decimal("100000"))
    assert ("000660", 0.0) in seen
