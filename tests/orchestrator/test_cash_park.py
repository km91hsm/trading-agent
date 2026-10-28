"""현금 단기금리 ETF(Stock D-115, 기본 끔): 하락 국면에만 남는 현금(예비 20% 제외)을 사고, 국면이
풀리면 전량 매도. 운영일 1회, 분석 대상에서 빠진다."""

from datetime import date
from decimal import Decimal

from conftest import make_account

from trader.broker.fake import FakeBroker
from trader.broker.models import Cash, Position
from trader.config import Settings
from trader.orchestrator import cash_park, executor

D = date(2026, 10, 27)
ON = Settings(cash_park={"enabled": True})


class Market:
    def get_price_kr(self, ticker):
        from types import SimpleNamespace

        return SimpleNamespace(price=Decimal("10000"))


def _ctx(conn, settings, positions=()):
    acct = make_account("paper-main", "paper", "auto")
    cash = {"KR": Cash(currency="KRW", available=Decimal(10_000_000), total=Decimal(10_000_000))}
    broker = FakeBroker(acct, cash=cash, positions=list(positions))
    return executor.ExecContext(
        settings=settings, accounts=[acct], conn=conn, brokers={"paper-main": broker}
    )


def test_off_by_default(conn):
    r = cash_park.run(
        _ctx(conn, Settings()), market_client=Market(), today=D, regime="bear", dry_run=True
    )
    assert r == {"skipped": "꺼짐"}


def test_bear_parks_spare_cash_once(conn, project):
    r = cash_park.run(_ctx(conn, ON), market_client=Market(), today=D, regime="bear", dry_run=True)
    (o,) = r["orders"]
    v = o["results"][0]
    assert o["action"] == "BUY" and v["approved"] and v["qty"] == 800  # (1000만 − 200만)/1만
    # dry_run 은 기록하지 않는다 — 같은 날 실제 실행을 막지 않게
    real = cash_park.run(
        _ctx(conn, ON), market_client=Market(), today=D, regime="bear", dry_run=False
    )
    assert real["orders"][0]["results"][0]["order"]["status"] == "accepted"
    again = cash_park.run(
        _ctx(conn, ON), market_client=Market(), today=D, regime="bear", dry_run=False
    )
    assert again == {"skipped": "오늘 이미 실행"}
    assert "357870" in ON.excluded_tickers("KR") and "357870" not in Settings().excluded_tickers(
        "KR"
    )


def test_unparks_when_not_bear(conn, project):
    held = Position(
        ticker="357870",
        market="KR",
        qty=50,
        avg_price=Decimal("10000"),
        current_price=Decimal("10000"),
        eval_amount=Decimal("500000"),
        pnl_pct=0.0,
    )
    r = cash_park.run(
        _ctx(conn, ON, [held]), market_client=Market(), today=D, regime="neutral", dry_run=True
    )
    (o,) = r["orders"]
    assert o["action"] == "SELL" and o["results"][0]["qty"] == 50
    # bear 도 아니고 보유도 없으면 아무 주문 없음
    r2 = cash_park.run(
        _ctx(conn, ON),
        market_client=Market(),
        today=date(2026, 10, 28),
        regime="bull",
        dry_run=True,
    )
    assert r2["orders"] == []


def test_approve_mode_accounts_skipped(conn, project):
    from trader.control import control

    ctx = _ctx(conn, ON)
    control.set_account_mode(conn, ctx.accounts[0], "approve")
    assert cash_park.run(ctx, market_client=Market(), today=D, regime="bear", dry_run=False) == {
        "skipped": "오늘 이미 실행"
    }


def test_one_account_failure_does_not_block_others(conn, project):
    from conftest import make_account

    good = make_account("paper-b", "paper", "auto")
    ctx = _ctx(conn, ON)
    cash = {"KR": Cash(currency="KRW", available=Decimal(10_000_000), total=Decimal(10_000_000))}
    ctx.accounts.append(good)
    ctx.brokers["paper-b"] = FakeBroker(good, cash=cash)
    ctx.brokers["paper-main"].fail = {"get_balance"}
    r = cash_park.run(ctx, market_client=Market(), today=D, regime="bear", dry_run=False)
    by = {o["account_id"]: o for o in r["orders"]}
    assert "error" in by["paper-main"] and by["paper-b"]["action"] == "BUY"


def test_same_day_regime_flip_cancels_open_park_buy_and_allows_reentry(conn, project):
    """2-O17: 하루 1회 done 이 같은 날 국면 전환의 반대 주문을 막았다 — 국면(목표 행동)별 상태로,
    bear 를 벗어나면 미체결 매수를 취소하고, 다시 bear 가 되면 새 세대로 다시 산다."""
    from datetime import UTC, datetime

    from trader.ledger import repo

    ctx = _ctx(conn, ON)
    ctx.now = lambda: datetime(2026, 10, 27, 1, 0, tzinfo=UTC)  # D 장중(주문 시각이 운영일 안)
    first = cash_park.run(ctx, market_client=Market(), today=D, regime="bear", dry_run=False)
    assert first["orders"][0]["results"][0]["order"]["status"] == "accepted"
    flip = cash_park.run(ctx, market_client=Market(), today=D, regime="neutral", dry_run=False)
    assert flip["orders"][0]["cancelled"] == 1  # 미체결 매수 취소, 보유 없음 → 매도 없음
    assert ctx.brokers["paper-main"].cancelled
    assert repo.open_orders_today(conn, "paper-main", "357870", "BUY", D, market="KR") == []
    again = cash_park.run(ctx, market_client=Market(), today=D, regime="bear", dry_run=False)
    assert again["orders"][0]["results"][0]["order"]["status"] == "accepted"  # 새 세대 — 키 별개
    assert cash_park.run(ctx, market_client=Market(), today=D, regime="bear", dry_run=False) == {
        "skipped": "오늘 이미 실행"
    }


def test_cancel_failure_keeps_flip_in_progress(conn, project):
    """2-O17 보완: 반대 방향 미체결을 취소하지 못하면 진행 중으로 남겨 다음 틱에 다시 본다."""
    from datetime import UTC, datetime

    ctx = _ctx(conn, ON)
    ctx.now = lambda: datetime(2026, 10, 27, 1, 0, tzinfo=UTC)
    cash_park.run(ctx, market_client=Market(), today=D, regime="bear", dry_run=False)
    ctx.brokers["paper-main"].fail = {"cancel_order"}
    flip = cash_park.run(ctx, market_client=Market(), today=D, regime="neutral", dry_run=False)
    assert flip["orders"][0]["cancel_failed"] == ["357870"]
    ctx.brokers["paper-main"].fail = set()
    retry = cash_park.run(ctx, market_client=Market(), today=D, regime="neutral", dry_run=False)
    assert retry["orders"][0]["cancelled"] == 1
