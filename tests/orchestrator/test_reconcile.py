"""야간 KIS 대조(Phase 21): 브로커 보유 vs 원장 스냅샷, 마감 뒤 미정리 주문. 브로커가 진실."""

from datetime import UTC, date, datetime
from decimal import Decimal

from conftest import make_account

from trader.broker.fake import FakeBroker
from trader.broker.models import Cash, Position
from trader.control import control
from trader.ledger import repo, views
from trader.orchestrator import reconcile

D = date(2026, 10, 24)
T0 = datetime(2026, 10, 24, 7, 40, tzinfo=UTC)


class Notes:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, text, **_):
        self.sent.append(text)
        return True


def _pos(ticker, qty):
    return Position(
        ticker=ticker,
        market="KR",
        qty=qty,
        avg_price=Decimal("1000"),
        current_price=Decimal("1100"),
        eval_amount=Decimal(1100 * qty),
        pnl_pct=0.1,
    )


def _broker(acct, positions):
    cash = {
        "KR": Cash(
            currency="KRW",
            available=Decimal("1000000"),
            total=Decimal("1000000"),
            nav=Decimal("2000000"),
        )
    }
    return FakeBroker(acct, cash=cash, positions=positions)


def test_compare_positions_reports_both_sides():
    out = reconcile.compare_positions(
        [{"ticker": "A", "qty": 10}, {"ticker": "B", "qty": 5}], [_pos("A", 10), _pos("C", 3)]
    )
    assert out == [
        {"ticker": "B", "ledger_qty": 5, "broker_qty": 0},
        {"ticker": "C", "ledger_qty": 0, "broker_qty": 3},
    ]


def test_reconcile_account_ok_when_ledger_matches(conn):
    acct = make_account("paper-main", "paper", "auto", ["KR"])
    repo.snapshot_positions(
        conn,
        account_id="paper-main",
        d=D,
        market="KR",
        nav=Decimal(1),
        cash=Decimal(1),
        positions=[{"ticker": "005930", "market": "KR", "qty": 10}],
    )
    notes = Notes()
    out = reconcile.reconcile_account(
        conn,
        _broker(acct, [_pos("005930", 10)]),
        acct,
        "KR",
        today=D,
        now=lambda: T0,
        notifier=notes,
    )
    assert out["ok"] and out["mismatches"] == [] and out["open_orders"] == []
    assert notes.sent == [] and not out["snapshot_fixed"]
    assert control.get_flag(conn, reconcile.flag_key("paper-main", "KR", D))["ok"] is True


def test_reconcile_mismatch_notifies_and_fixes_snapshot_but_keeps_orders(conn):
    acct = make_account("paper-main", "paper", "auto", ["KR"])
    repo.snapshot_positions(
        conn,
        account_id="paper-main",
        d=D,
        market="KR",
        nav=Decimal(1),
        cash=Decimal(1),
        positions=[{"ticker": "005930", "market": "KR", "qty": 10}],
    )
    did = repo.insert_hedge_decision(conn, D, "KR", "005930", {"action": "BUY"})
    oid = repo.insert_order(
        conn,
        decision_id=did,
        account_id="paper-main",
        ticker="005930",
        market="KR",
        side="BUY",
        qty=10,
        order_type="MARKET",
        price=None,
        order_no="X1",
        status="accepted",
        message="",
        mode="auto",
    )
    notes = Notes()
    out = reconcile.reconcile_account(
        conn,
        _broker(acct, [_pos("005930", 7), _pos("000660", 2)]),
        acct,
        "KR",
        today=D,
        now=lambda: T0,
        notifier=notes,
        label=lambda t: f"[{t}]",
    )
    assert not out["ok"] and out["snapshot_fixed"]
    assert out["mismatches"] == [
        {"ticker": "000660", "ledger_qty": 0, "broker_qty": 2},
        {"ticker": "005930", "ledger_qty": 10, "broker_qty": 7},
    ]
    assert [o["id"] for o in out["open_orders"]] == [oid]
    assert len(notes.sent) == 1 and notes.sent[0].startswith(
        "[대조 불일치] paper-main KR 2026-10-24"
    )
    assert "[005930] 원장 10↔KIS 7" in notes.sent[0] and f"주문 #{oid} BUY" in notes.sent[0]
    latest = views.latest_positions(conn, "paper-main", "KR")
    assert {p["ticker"]: p["qty"] for p in latest["positions"]} == {"005930": 7, "000660": 2}
    assert latest["nav"] == "2000000"
    # 주문 행은 그대로(추적용)
    assert (
        conn.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()["status"]
        == "accepted"
    )


def test_reconcile_all_isolates_failures_and_filters_market(conn):
    a1 = make_account("paper-main", "paper", "auto", ["KR", "US"])
    a2 = make_account("real-main", "real", "off", ["KR"])
    b1 = _broker(a1, [])
    b2 = _broker(a2, [])
    b2.fail = {"get_balance", "get_positions"}
    out = reconcile.reconcile_all(
        conn,
        {"paper-main": b1, "real-main": b2},
        [a1, a2],
        market="KR",
        today_for=lambda m: D,
        now=lambda: T0,
    )
    assert out["paper-main:KR"]["ok"] and "error" in out["real-main:KR"]
    assert "paper-main:US" not in out
