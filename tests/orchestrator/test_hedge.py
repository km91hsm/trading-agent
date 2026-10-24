"""국면 헤지(orchestrator/hedge.rebalance)·executor is_hedge·regime 게이트 연결."""

from datetime import UTC, date, datetime
from decimal import Decimal

from helpers_agents import ok_decision

from trader.agents.schemas import Decision
from trader.broker.fake import FakeBroker
from trader.broker.models import Cash, Position
from trader.config import AccountConfig, HedgeCfg, Settings
from trader.control import control
from trader.ledger import metrics, repo
from trader.orchestrator import executor, hedge
from trader.orchestrator.run_store import read_checkpoint, run_dir
from trader.risk import regime

D = date(2026, 10, 24)
T0 = datetime(2026, 10, 24, 0, 30, tzinfo=UTC)
CFG = HedgeCfg(ma_days=20, momentum_days=5, exit_streak_days=1)


class Notes:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, text, *, escape=True):
        self.sent.append(text)
        return True


def _cash():
    return {
        "KR": Cash(currency="KRW", available=Decimal("10000000"), total=Decimal("10000000")),
        "US": Cash(currency="USD", available=Decimal("10000"), total=Decimal("10000")),
    }


def _ctx(conn, *, positions=None, real_mode="off"):
    accounts = [
        AccountConfig(id="paper-main", env="paper", cano="1", markets=["KR", "US"], mode="auto"),
        AccountConfig(id="real-main", env="real", cano="2", markets=["KR"], mode=real_mode),
    ]
    brokers = {a.id: FakeBroker(a, cash=_cash(), positions=list(positions or [])) for a in accounts}
    settings = Settings(hedge=CFG)
    return executor.ExecContext(
        settings=settings,
        accounts=accounts,
        conn=conn,
        brokers=brokers,
        notifier=Notes(),
        now=lambda: T0,
    )


def _set_regime(conn, state, count=2):
    control.set_flag(
        conn,
        "regime:KR",
        {
            "state": state,
            "count": count,
            "signals": {"ma_below": True, "momentum_weak": count >= 2, "vol_high": count >= 3},
        },
    )


def test_bear_enters_inverse_per_account_and_records_hedge_decision(project, conn):
    ctx = _ctx(conn)
    _set_regime(conn, "bear", 2)
    out = hedge.rebalance(ctx, market="KR", today=D, cfg=CFG, quote_fn=lambda i: Decimal("5000"))
    assert out["action"] == "enter" and out["ticker"] == "114800"  # 2/3 → −1x
    paper = next(r for r in out["results"] if r["account_id"] == "paper-main")
    assert paper["approved"] and paper["qty"] == 400  # 1000만 × 20% / 5000
    real = next(r for r in out["results"] if r["account_id"] == "real-main")
    assert real["mode"] == "off" and real["order"] is None  # 실전 off 는 기록만
    orders = ctx.brokers["paper-main"].orders
    assert len(orders) == 1 and orders[0].side == "BUY" and orders[0].order_type == "MARKET"
    assert read_checkpoint(run_dir(D, "KR", "114800"), "46_hedge_paper-main")["data"]["approved"]
    assert read_checkpoint(run_dir(D, "KR", "114800"), "56_order_hedge_paper-main")
    row = conn.execute("SELECT source, action FROM decisions WHERE ticker='114800'").fetchone()
    assert row["source"] == "hedge" and row["action"] == "BUY"
    assert metrics.decision_distribution(conn, days=30, now=T0) == {}  # 분포에서 제외
    assert any("[헤지 진입]" in m for m in ctx.notifier.sent)
    # 같은 날 재실행은 건너뛴다
    assert hedge.rebalance(ctx, market="KR", today=D, cfg=CFG, quote_fn=lambda i: Decimal(1)) == {
        "skipped": "오늘 이미 실행"
    }


def test_three_signals_pick_leveraged_and_exit_sells_all_when_regime_clears(project, conn):
    ctx = _ctx(conn)
    _set_regime(conn, "bear", 3)
    out = hedge.rebalance(ctx, market="KR", today=D, cfg=CFG, quote_fn=lambda i: Decimal("3000"))
    assert out["ticker"] == "252670"
    # 다음 날: 보유 중인데 국면이 neutral → 전량 SELL (최소 보유·주 2회 면제)
    pos = Position(
        ticker="252670",
        market="KR",
        qty=600,
        avg_price=Decimal("3000"),
        current_price=Decimal("3100"),
        eval_amount=Decimal("1860000"),
        pnl_pct=0.03,
    )
    ctx2 = _ctx(conn, positions=[pos])
    _set_regime(conn, "neutral", 1)
    d2 = date(2026, 10, 25)
    out2 = hedge.rebalance(ctx2, market="KR", today=d2, cfg=CFG, quote_fn=lambda i: Decimal("3100"))
    assert out2["action"] == "exit" and out2["held"] == ["252670"]
    paper = next(r for r in out2["results"] if r["account_id"] == "paper-main")
    assert paper["approved"] and paper["qty"] == 600
    sells = [o for o in ctx2.brokers["paper-main"].orders if o.side == "SELL"]
    assert len(sells) == 1 and sells[0].ticker == "252670"
    # bear 가 아니고 보유도 없으면 아무것도 하지 않는다
    ctx3 = _ctx(conn)
    assert (
        hedge.rebalance(
            ctx3, market="KR", today=date(2026, 10, 26), cfg=CFG, quote_fn=lambda i: Decimal(1)
        )["action"]
        == "none"
    )


def test_dry_run_records_only_and_does_not_mark_done(project, conn):
    ctx = _ctx(conn)
    _set_regime(conn, "bear", 2)
    out = hedge.rebalance(
        ctx, market="KR", today=D, cfg=CFG, quote_fn=lambda i: Decimal("5000"), dry_run=True
    )
    assert out["action"] == "enter" and ctx.brokers["paper-main"].orders == []
    assert control.get_flag(conn, hedge.done_key("KR", D)) is None
    assert read_checkpoint(run_dir(D, "KR", "114800"), "46_hedge_paper-main")["data"]["dry_run"]


def test_quote_failure_is_notified_and_marks_done(project, conn):
    ctx = _ctx(conn)
    _set_regime(conn, "bear", 2)

    def boom(i):
        raise ValueError("시세 없음")

    out = hedge.rebalance(ctx, market="KR", today=D, cfg=CFG, quote_fn=boom)
    assert out["results"][0]["error"].startswith("시세 실패") and any(
        "[헤지 보류]" in m for m in ctx.notifier.sent
    )


def test_regime_bear_blocks_normal_buy_and_hedge_positions_dont_count(project, conn):
    hedge_pos = Position(
        ticker="114800",
        market="KR",
        qty=400,
        avg_price=Decimal("5000"),
        current_price=Decimal("5000"),
        eval_amount=Decimal("2000000"),
        pnl_pct=0.0,
    )
    ctx = _ctx(conn, positions=[hedge_pos])
    _set_regime(conn, "bear", 2)
    dec = Decision(**ok_decision())
    repo.ingest_checkpoint(conn, D, "KR", "005930", "30_decision", {"data": dec.model_dump()})
    res = executor.execute(ctx, dec, date=D, market="KR", price=Decimal("70000"), dry_run=False)
    paper = next(r for r in res if r["account_id"] == "paper-main")
    assert not paper["approved"] and any("bear" in s for s in paper["reasons"])
    assert "국면 bear" in paper["note"]
    # neutral 이면 한도 축소 사유와 함께 통과
    _set_regime(conn, "neutral", 1)
    res = executor.execute(ctx, dec, date=D, market="KR", price=Decimal("70000"), dry_run=True)
    paper = next(r for r in res if r["account_id"] == "paper-main")
    assert paper["approved"] and any("neutral" in s for s in paper["reasons"])


def test_regime_state_defaults_to_bull_when_disabled_or_missing(conn):
    ctx = _ctx(conn)
    assert executor.regime_state(ctx, "KR") == "bull"
    regime.update(
        conn, "KR", closes=[100.0] * 30 + [95.0, 90.0, 85.0, 80.0, 75.0, 70.0], cfg=CFG, today=D
    )
    assert executor.regime_state(ctx, "KR") == "bear"
    ctx.settings = Settings(hedge=HedgeCfg(enabled=False))
    assert executor.regime_state(ctx, "KR") == "bull"


# --- Phase 18: 시장 세션 제안 반영 -------------------------------------------------


def _view(conn, **over):
    v = {
        "date": D.isoformat(),
        "market": "KR",
        "stance": "RISK_OFF",
        "hedge_weight": 0.12,
        "leveraged": True,
        "confidence": 0.7,
    }
    v.update(over)
    control.set_flag(conn, "market_view:KR", v)


def test_proposal_weight_is_used_within_bounds(project, conn):
    ctx = _ctx(conn)
    _set_regime(conn, "bear", 2)
    _view(conn, hedge_weight=0.12)
    out = hedge.rebalance(ctx, market="KR", today=D, cfg=CFG, quote_fn=lambda i: Decimal("5000"))
    assert out["action"] == "enter" and out["weight"] == 0.12
    assert out["view"]["stance"] == "RISK_OFF"
    paper = next(r for r in out["results"] if r["account_id"] == "paper-main")
    assert paper["qty"] == 240  # 1000만 × 12% / 5000
    dec = repo.latest_decision(conn, "KR", "114800")
    assert "시장 세션 제안 RISK_OFF 12%" in dec["data"]["rationale"]


def test_proposal_below_min_is_raised_to_min(project, conn):
    ctx = _ctx(conn)
    _set_regime(conn, "bear", 2)
    _view(conn, hedge_weight=0.01)
    out = hedge.rebalance(ctx, market="KR", today=D, cfg=CFG, quote_fn=lambda i: Decimal("5000"))
    assert out["weight"] == 0.05
    paper = next(r for r in out["results"] if r["account_id"] == "paper-main")
    assert paper["qty"] == 100


def test_proposal_can_refuse_leverage_but_not_force_entry(project, conn):
    ctx = _ctx(conn)
    _set_regime(conn, "bear", 3)
    _view(conn, leveraged=False)
    out = hedge.rebalance(ctx, market="KR", today=D, cfg=CFG, quote_fn=lambda i: Decimal("5000"))
    assert out["ticker"] == "114800"  # 3/3 이지만 제안이 레버리지 거부 → −1x
    # bear 가 아니면 RISK_OFF 제안이 와도 진입하지 않는다 (규칙 우선)
    ctx2 = _ctx(conn)
    _set_regime(conn, "neutral", 1)
    _view(conn, date=date(2026, 10, 25).isoformat(), hedge_weight=0.2)
    out2 = hedge.rebalance(
        ctx2, market="KR", today=date(2026, 10, 25), cfg=CFG, quote_fn=lambda i: Decimal("5000")
    )
    assert out2["action"] == "none" and out2["view"]["hedge_weight"] == 0.2


def test_stale_proposal_is_ignored(project, conn):
    ctx = _ctx(conn)
    _set_regime(conn, "bear", 2)
    _view(conn, date="2026-10-23", hedge_weight=0.05)  # 어제 제안
    out = hedge.rebalance(ctx, market="KR", today=D, cfg=CFG, quote_fn=lambda i: Decimal("5000"))
    assert out["view"] is None and out["weight"] == 0.2
