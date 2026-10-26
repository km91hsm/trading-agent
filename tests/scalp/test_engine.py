"""단타 집행(Phase 23 S): 모의 전용·bear/RISK_OFF 금지·종목당 1회·청산·일일 중단·dry_run·상태."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from conftest import make_account

from trader.broker.fake import FakeBroker, FakeMarket
from trader.broker.models import Candle, Cash, Fill, Position
from trader.config import HedgeCfg, ScalpCfg, Settings
from trader.control import control
from trader.orchestrator import run_store
from trader.orchestrator.run_store import read_checkpoint, run_dir
from trader.scalp import engine

D = date(2026, 10, 24)
NOW = datetime(2026, 10, 24, 1, 0, tzinfo=UTC)  # 10:00 KST


class Notes:
    def __init__(self):
        self.msgs = []

    def send(self, text, *, escape=True):
        self.msgs.append(text)
        return True


def _bars(burst: bool, price="10000"):
    out = []
    for i in range(55):
        h, m = divmod(9 * 60 + i, 60)
        p = Decimal(price)
        vol = 100
        if burst and i >= 50:
            vol = 400
            p = Decimal(price) + (Decimal(10) * (i - 49) if i == 54 else Decimal(0))
        out.append(
            Candle(date=D, open=p, high=p, low=p, close=p, volume=vol, time=f"{h:02d}{m:02d}00")
        )
    return out


def _fill(order_no, side, qty, price, at):
    return Fill(
        order_no=order_no,
        ticker="005930",
        market="KR",
        side=side,
        qty=qty,
        price=Decimal(price),
        filled_at=at,
    )


def fill_all(ctx, conn, price=None):
    """접수된 주문을 전부 체결시킨다(모의 브로커 체결 조회에 반영 — 다음 틱의 동기화가 읽는다).
    가격은 지정가, 시장가는 price(없으면 현재가)."""
    for o in conn.execute("SELECT * FROM orders WHERE status IN ('accepted','partial')"):
        px = o["price"] or price or str(ctx.market_client.prices[o["ticker"]])
        b = ctx.brokers[o["account_id"]]
        fills = b.fills.setdefault(("KR", D), [])
        if any(f.order_no == o["order_no"] for f in fills):
            continue
        fills.append(
            Fill(
                order_no=o["order_no"],
                ticker=o["ticker"],
                market="KR",
                side=o["side"],
                qty=o["qty"],
                price=Decimal(str(px)),
                filled_at=ctx.now(),
            )
        )
        held = {x.ticker: x.qty for x in b.positions}
        q = held.get(o["ticker"], 0) + (o["qty"] if o["side"] == "BUY" else -o["qty"])
        others = [x for x in b.positions if x.ticker != o["ticker"]]
        p = Decimal(str(px))
        b.positions = others + (
            [
                Position(
                    ticker=o["ticker"],
                    market="KR",
                    qty=q,
                    avg_price=p,
                    current_price=p,
                    eval_amount=p * q,
                    pnl_pct=0.0,
                )
            ]
            if q > 0
            else []
        )


def _ctx(
    conn, *, minutes=None, prices=None, accounts=None, enabled=True, hedge=None, dry_run=False
):
    accounts = accounts or [
        make_account("paper-main", "paper", "auto", ["KR"]),
        make_account("real-main", "real", "off", ["KR"]),
    ]
    cash = {"KR": Cash(currency="KRW", available=Decimal("10000000"), total=Decimal("10000000"))}
    brokers = {a.id: FakeBroker(a, cash=cash) for a in accounts}
    settings = Settings(scalp=ScalpCfg(enabled=enabled), hedge=hedge or HedgeCfg())
    mc = FakeMarket(prices=prices or {}, minutes=minutes or {}, now=lambda: NOW)
    ctx = engine.ScalpContext(
        settings=settings,
        accounts=accounts,
        conn=conn,
        brokers=brokers,
        market_client=mc,
        open_hm="09:00",
        close_hm="15:30",
        notifier=Notes(),
        now=lambda: NOW,
        dry_run=dry_run,
    )
    return ctx


def _scan(picks):
    run_store.write_checkpoint(
        run_store.market_dir(D, "KR", create=True),
        "_scan",
        {"picks": [{"ticker": t} for t in picks], "analyze_targets": []},
    )


def test_tick_enters_on_signal_paper_only_and_once_per_ticker(conn, project):
    _scan(["005930", "000660"])
    ctx = _ctx(conn, minutes={"005930": _bars(True), "000660": _bars(False)})
    out = engine.tick(ctx, market="KR", today=D)
    assert out["candidates"] == 2 and [e["ticker"] for e in out["entries"]] == ["005930"]
    assert out["entries"][0]["qty"] == {"paper-main": 49}  # 1천만 × 5% / 10050 지정가
    st = engine.load_state(None, D, "KR")
    assert st["open"][0]["ticker"] == "005930" and st["done"] == ["005930"]
    # 실전 계좌는 건너뛴다: 체크포인트는 모의 계좌만
    d = run_dir(D, "KR", "005930")
    assert read_checkpoint(d, "47_scalp_entry_paper-main")["data"]["approved"] is True
    assert read_checkpoint(d, "47_scalp_entry_real-main") is None
    assert read_checkpoint(d, "57_order_scalp_entry_paper-main")["data"]["status"] == "accepted"
    # decisions 행은 만들지 않는다 (오늘 LLM 결정과 키가 겹치므로)
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
    row = conn.execute("SELECT decision_id, side, qty FROM orders").fetchone()
    assert row["decision_id"] is None and row["side"] == "BUY"
    # 그날 첫 묶음은 바로 — 진입은 '[단타]' 묶음 한 통 안의 한 줄 (전체 검토 T3)
    assert [m.splitlines()[1] for m in ctx.notifier.msgs if m.startswith("[단타]")] == [
        "진입 005930 @10,050원 · " + engine.load_state(None, D, "KR")["open"][0]["reason"]
    ]
    # 다음 틱: 같은 종목은 보유 중이라 후보에서 빠지고, 신호가 있어도 다시 사지 않는다
    out2 = engine.tick(ctx, market="KR", today=D)
    assert out2["entries"] == [] and out2["candidates"] == 1


def test_tick_exits_on_take_profit_and_halts_on_daily_stop(conn, project):
    _scan(["005930"])
    ctx = _ctx(conn, minutes={"005930": _bars(True)}, prices={"005930": Decimal("10050")})
    engine.tick(ctx, market="KR", today=D)
    # 체결됐다고 치고 모의 브로커 보유에 반영 (매도 수량은 브로커 잔고에서 온다)
    ctx.brokers["paper-main"].positions = [
        Position(
            ticker="005930",
            market="KR",
            qty=49,
            avg_price=Decimal("10050"),
            current_price=Decimal("10300"),
            eval_amount=Decimal("10300") * 49,
            pnl_pct=2.5,
        )
    ]
    fills = ctx.brokers["paper-main"].fills
    fills[("KR", D)] = [_fill("FAKE-1", "BUY", 49, "10050", NOW + timedelta(minutes=1))]
    ctx.market_client.prices["005930"] = Decimal("10300")  # +2.5% ≥ 1.5%
    later = NOW + timedelta(minutes=5)
    ctx.now = lambda: later
    out = engine.tick(ctx, market="KR", today=D)
    assert out["exits"] == []  # 청산 주문 접수 — 체결을 기다린다
    fills[("KR", D)].append(_fill("FAKE-2", "SELL", 49, "10300", later))
    out = engine.tick(ctx, market="KR", today=D)
    assert out["exits"] == [
        {"ticker": "005930", "reason": "take_profit", "ret": pytest.approx(0.0249, abs=1e-3)}
    ]
    # 첫 묶음 5분 뒤 청산은 묶음 주기(30분) 안이라 모아 둔다 — 틱마다 오지 않는다
    assert sum(m.startswith("[단타]") for m in ctx.notifier.msgs) == 1
    assert engine.load_state(None, D, "KR")["outbox"][0]["leg"] == "exit"
    st = engine.load_state(None, D, "KR")
    assert st["open"] == [] and st["closed"][0]["exit_price"] == "10300"
    sells = conn.execute("SELECT side, order_type FROM orders WHERE side='SELL'").fetchall()
    assert len(sells) == 1 and sells[0]["order_type"] == "MARKET"
    assert read_checkpoint(run_dir(D, "KR", "005930"), "57_order_scalp_exit_paper-main")
    # 손실 누적으로 일일 한도: 청산 수익률 × 비중 합 ≤ −1% → 중단
    st["closed"] = [{"ret": -0.11, "ticker": "x"}, {"ret": -0.1, "ticker": "y"}]
    engine.save_state(None, D, "KR", st)
    _scan(["000660"])
    ctx.market_client.minutes["000660"] = _bars(True)
    out3 = engine.tick(ctx, market="KR", today=D)
    assert out3["entries"] == [] and "실현" in engine.load_state(None, D, "KR")["halted"]
    assert "skipped" in engine.tick(ctx, market="KR", today=D)
    assert any("[단타 중단]" in m for m in ctx.notifier.msgs)
    # 중단 때는 모아 둔 청산을 즉시 보낸다 (한글 사유·영문 코드 없음)
    digest = [m for m in ctx.notifier.msgs if m.startswith("[단타]")][-1]
    assert "청산 005930 @10,300원 · 익절" in digest and "take_profit" not in digest
    assert engine.load_state(None, D, "KR")["outbox"] == []


def test_allowed_blocks_disabled_paused_bear_and_strategist(conn, project):
    ctx = _ctx(conn, enabled=False)
    assert engine.allowed(ctx, "KR", D, {}) == "scalp.enabled=false"
    ctx = _ctx(conn)
    control.set_flag(conn, "paused", True)
    assert engine.allowed(ctx, "KR", D, {}) == "일시정지"
    control.set_flag(conn, "paused", False)
    control.set_flag(conn, "regime:KR", {"state": "bear", "count": 3})
    assert "bear" in engine.allowed(ctx, "KR", D, {})
    control.set_flag(conn, "regime:KR", {"state": "neutral", "count": 1})
    view = {"date": D.isoformat(), "stance": "NEUTRAL", "confidence": 0.9, "scalp_ok": False}
    control.set_flag(conn, "market_view:KR", view)
    assert "scalp_ok" in engine.allowed(ctx, "KR", D, {})
    control.set_flag(conn, "market_view:KR", view | {"scalp_ok": True, "stance": "RISK_OFF"})
    assert "RISK_OFF" in engine.allowed(ctx, "KR", D, {})
    control.set_flag(conn, "market_view:KR", view | {"scalp_ok": False, "confidence": 0.3})
    assert engine.allowed(ctx, "KR", D, {}) is None  # 확신 미달 제안은 무시
    # 모의 계좌가 없으면 금지
    ctx2 = _ctx(conn, accounts=[make_account("real-main", "real", "off", ["KR"])])
    assert "모의" in engine.allowed(ctx2, "KR", D, {})


def test_candidates_exclude_held_hedge_and_include_news_watch(conn, project):
    from trader import jobs as jobq

    _scan(["005930", "000660"])
    ctx = _ctx(conn, hedge=HedgeCfg(enabled=True))
    pos = Position(
        ticker="000660",
        market="KR",
        qty=1,
        avg_price=Decimal(1),
        current_price=Decimal(1),
        eval_amount=Decimal(1),
        pnl_pct=0.0,
    )
    ctx.brokers["paper-main"].positions = [pos]
    jobq.enqueue(
        conn, "analyze_ticker", {"market": "KR", "ticker": "247540"}, source="news", now=NOW
    )
    control.set_watchlist(conn, ["KR:035420", "US:AAPL"])
    hedge_t = next(iter(ctx.settings.hedge.tickers("KR")))
    control.set_watchlist(conn, ["KR:035420", f"KR:{hedge_t}"])
    cands = engine.candidates(ctx, "KR", D, {"open": [], "done": ["247540"]})
    assert [c["ticker"] for c in cands] == ["005930", "035420"]


def test_dry_run_records_without_orders(conn, project):
    _scan(["005930"])
    ctx = _ctx(conn, minutes={"005930": _bars(True)}, dry_run=True)
    out = engine.tick(ctx, market="KR", today=D)
    assert out["entries"][0]["qty"] == {"paper-main": 49}
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert engine.load_state(None, D, "KR")["open"][0]["dry_run"] is True
    rep = engine.report(None, market="KR", days=3, today=D)
    assert rep["days"][0]["date"] == D.isoformat() and rep["summary"]["trades"] == 0


def test_digest_every_min_bundles_and_dry_run_is_korean(conn, project):
    """전체 검토 T3: 묶음 주기가 지나면 모인 알림을 한 통으로, dry_run 은 '주문 없이 판정'."""
    _scan(["005930", "000660"])
    ctx = _ctx(conn, minutes={"005930": _bars(True)}, dry_run=True)
    engine.tick(ctx, market="KR", today=D)
    assert len(ctx.notifier.msgs) == 1 and "(주문 없이 판정)" in ctx.notifier.msgs[0]
    assert "dry-run" not in ctx.notifier.msgs[0]
    ctx.market_client.minutes["000660"] = _bars(True)
    ctx.now = lambda: NOW + timedelta(minutes=10)
    engine.tick(ctx, market="KR", today=D)
    assert len(ctx.notifier.msgs) == 1  # 주기 안 — 보류
    ctx.now = lambda: NOW + timedelta(minutes=31)
    engine.tick(ctx, market="KR", today=D)
    assert len(ctx.notifier.msgs) == 2 and "진입 000660" in ctx.notifier.msgs[1]


def test_digest_kept_when_send_fails(conn, project):
    """Codex 리뷰: 텔레그램 실패(False)·예외면 묶음을 버리지 않고 다음 틱에 다시 보낸다."""
    _scan(["005930"])
    ctx = _ctx(conn, minutes={"005930": _bars(True)})
    ctx.notifier.send = lambda text, *, escape=True: False
    engine.tick(ctx, market="KR", today=D)
    st = engine.load_state(None, D, "KR")
    assert len(st["outbox"]) == 1 and not st.get("digest_at")
    sent = []
    ctx.notifier.send = lambda text, *, escape=True: sent.append(text) or True
    ctx.now = lambda: NOW + timedelta(minutes=1)
    engine.tick(ctx, market="KR", today=D)
    assert len(sent) == 1 and "진입 005930" in sent[0]
    assert engine.load_state(None, D, "KR")["outbox"] == []
