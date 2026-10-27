"""Phase 26 단계 2 Task 2.7: 잔고 갱신·야간 대조가 `account.asset` 로 브로커 타입을 가른다
(stock → 현물 `build_portfolio`·`reconcile_account`, fo → `fo.balance.refresh/reconcile`)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from conftest import make_account

from trader.broker.fake import FakeBroker, FakeFoBroker
from trader.broker.models import Cash, FoBalance, FoCash, FoPosition
from trader.config import AccountConfig, Settings
from trader.control import control
from trader.fo import store
from trader.orchestrator import balance, reconcile

NOW = datetime(2026, 10, 27, 1, 30, tzinfo=UTC)
NAV = Decimal("100000000")


class Spy:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str, *, escape: bool = True) -> bool:
        self.sent.append(text)
        return True


def fo_account() -> AccountConfig:
    return AccountConfig(
        id="paper-fo", env="paper", cano="00000000", acnt_prdt_cd="03", markets=["KR"],
        mode="auto", asset="fo",
    )  # fmt: skip


def fo_balance(positions=()) -> FoBalance:
    z = Decimal(0)
    cash = FoCash(
        deposit=NAV, orderable_cash=Decimal("50000000"), orderable_total=Decimal("50000000"),
        margin_total=Decimal("1000000"), add_margin=z, nav=NAV, realized_today=z, unrealized=z,
        requested_at=NOW, received_at=NOW,
    )  # fmt: skip
    return FoBalance(positions=list(positions), unverified=[], cash=cash)


def stock_broker(acct):
    return FakeBroker(
        acct, positions=[], cash={"KR": Cash(currency="KRW", available=Decimal(1000),
                                             total=Decimal(1000), nav=Decimal(1000))}
    )  # fmt: skip


def test_balance_refresh_dispatch_by_asset(conn, project):
    stock = make_account("paper-main", "paper", "auto", ["KR"])
    fo = fo_account()
    sb, fb = stock_broker(stock), FakeFoBroker(fo, balance=fo_balance([FoPosition(
        code="105W11", name="105W11", kind="FUTURES", contract_side="SHORT", qty=2,
        avg_price=Decimal("350"), current_price=Decimal("351"), pnl=Decimal("-100000"),
        sellable_qty=2,
    )]), now=lambda: NOW)  # fmt: skip
    out = balance.refresh(
        conn, accounts=[stock, fo], brokers={stock.id: sb, fo.id: fb}, market="KR",
        settings=Settings(), now=NOW, state=project / "state",
    )  # fmt: skip
    assert out["refreshed"] == ["paper-main", "paper-fo"] and out["errors"] == {}
    assert "get_balance" in sb.calls and fb.calls == ["get_balance"]
    nav = conn.execute("SELECT nav, margin_total FROM fo_nav_snapshot").fetchone()
    assert (Decimal(nav["nav"]), Decimal(nav["margin_total"])) == (NAV, Decimal("1000000"))
    snap = conn.execute(
        "SELECT code, contract_side, qty, delta_notional FROM fo_positions_snapshot"
    )
    rows = snap.fetchall()
    assert (rows[0]["code"], rows[0]["contract_side"], rows[0]["qty"]) == ("105W11", "SHORT", 2)
    assert Decimal(rows[0]["delta_notional"]) == -2 * Decimal("351") * 50000
    # 현물 스냅샷에 파생 계좌가 섞이지 않는다
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM positions_snapshot WHERE account_id='paper-fo'"
        ).fetchone()[0]
        == 0
    )
    # only_stale: 방금 쓴 파생 스냅샷은 건너뛴다
    conn.execute("UPDATE fo_nav_snapshot SET created_at=?", (NOW.isoformat(),))
    conn.commit()
    out = balance.refresh(
        conn, accounts=[fo], brokers={fo.id: fb}, market="KR", settings=Settings(), now=NOW,
        state=project / "state", only_stale=True,
    )  # fmt: skip
    assert out["skipped"] == ["paper-fo"] and fb.calls == ["get_balance"]


def test_balance_refresh_fo_error_isolated(conn, project):
    fo = fo_account()
    fb = FakeFoBroker(fo, balance=fo_balance(), fail={"get_balance"}, now=lambda: NOW)
    out = balance.refresh(
        conn, accounts=[fo], brokers={fo.id: fb}, market="KR", settings=Settings(), now=NOW,
        state=project / "state",
    )  # fmt: skip
    assert out["refreshed"] == [] and "paper-fo" in out["errors"]


def test_reconcile_fo_uses_recover(conn):
    """fo 계좌의 대조는 `positions.recover` — 브로커에 있는데 원장에 없는 보유는 `unknown`
    (미확정)으로 보고하고 알림·control 플래그를 남긴다. 현물 계좌는 기존 경로."""
    fo = fo_account()
    stock = make_account("paper-main", "paper", "auto", ["KR"])
    held = FoPosition(
        code="105W11", name="105W11", kind="FUTURES", contract_side="LONG", qty=2,
        avg_price=Decimal("350"), current_price=Decimal("351"), pnl=Decimal(0), sellable_qty=2,
    )  # fmt: skip
    fb = FakeFoBroker(fo, balance=fo_balance([held]), now=lambda: NOW)
    notes = Spy()
    out = reconcile.reconcile_all(
        conn, {fo.id: fb, stock.id: stock_broker(stock)}, [fo, stock], market=None,
        today_for=lambda m: date(2026, 10, 27), now=lambda: NOW, notifier=notes,
    )  # fmt: skip
    assert set(out) == {"paper-fo:KR", "paper-main:KR"}
    r = out["paper-fo:KR"]
    assert r["market"] == "KR" and r["mismatches"], r
    assert control.get_flag(conn, "reconcile:paper-fo:KR:2026-10-27") is not None
    assert any("[대조 불일치]" in t and "paper-fo" in t for t in notes.sent)
    assert "get_balance" in fb.calls
    assert [p.status for p in store.open_positions(conn, "paper-fo")] == ["unknown"]
    # 시장 필터 US 면 파생(KR 전용)은 건너뛴다
    out = reconcile.reconcile_all(
        conn, {fo.id: fb}, [fo], market="US", today_for=lambda m: date(2026, 10, 27),
        now=lambda: NOW,
    )  # fmt: skip
    assert out == {}
