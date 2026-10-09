from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from helpers_agents import ok_decision

from trader.agents.schemas import Decision
from trader.broker.fake import FakeBroker
from trader.broker.models import Cash
from trader.config import AccountConfig, Settings
from trader.control import control
from trader.ledger import repo
from trader.orchestrator import executor
from trader.orchestrator.run_store import read_checkpoint, run_dir

D = date(2026, 10, 9)
T0 = datetime(2026, 10, 9, 1, 0, tzinfo=UTC)
PRICE = Decimal("71500")


class FakeNotifier:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, text: str, *, escape: bool = True) -> bool:
        self.sent.append(text)
        return True


def _accounts():
    return [
        AccountConfig(id="paper-main", env="paper", cano="1", markets=["KR", "US"], mode="auto"),
        AccountConfig(id="real-main", env="real", cano="2", markets=["KR"], mode="off"),
    ]


def _broker(a, **kw):
    return FakeBroker(
        a,
        cash={
            "KR": Cash(currency="KRW", available=Decimal("10000000"), total=Decimal("10000000")),
            "US": Cash(currency="USD", available=Decimal("10000"), total=Decimal("10000")),
        },
        **kw,
    )


@pytest.fixture
def ectx(project, conn):
    accounts = _accounts()
    brokers = {a.id: _broker(a) for a in accounts}
    return executor.ExecContext(
        settings=Settings(),
        accounts=accounts,
        conn=conn,
        brokers=brokers,
        notifier=FakeNotifier(),
        now=lambda: T0,
    )


def _run(ectx, decision=None, *, dry_run=False, market="KR", exchange=None, price=PRICE):
    dec = Decision(**(decision or ok_decision()))
    repo.ingest_checkpoint(
        ectx.conn, D, market, dec.ticker, "30_decision", {"data": dec.model_dump()}
    )
    return executor.execute(
        ectx, dec, date=D, market=market, price=price, exchange=exchange, dry_run=dry_run
    )


def test_paper_auto_places_order_and_writes_checkpoints(ectx):
    results = _run(ectx)
    by = {r["account_id"]: r for r in results}
    assert by["paper-main"]["approved"] and by["paper-main"]["qty"] == 13  # 100만/71500
    orders = ectx.brokers["paper-main"].orders
    assert len(orders) == 1 and orders[0].qty == 13 and orders[0].price == Decimal("71000")
    rc = read_checkpoint(run_dir(D, "KR", "005930"), "40_risk_check_paper-main")
    assert rc["data"]["approved"] is True and rc["data"]["mode"] == "auto"
    oc = read_checkpoint(run_dir(D, "KR", "005930"), "50_order_paper-main")
    assert oc["data"]["result"]["accepted"] and oc["data"]["request"]["qty"] == 13
    row = ectx.conn.execute("SELECT status, order_no, mode FROM orders").fetchone()
    assert (row["status"], row["order_no"], row["mode"]) == ("accepted", "FAKE-1", "auto")
    assert ectx.conn.execute("SELECT COUNT(*) FROM risk_checks").fetchone()[0] == 2
    assert any("주문" in m and "paper-main" in m for m in ectx.notifier.sent)
    assert ectx.conn.execute("SELECT COUNT(*) FROM positions_snapshot").fetchone()[0] >= 1


def test_real_off_records_only(ectx):
    results = _run(ectx)
    real = next(r for r in results if r["account_id"] == "real-main")
    assert real["mode"] == "off" and real["order"] is None
    rc = read_checkpoint(run_dir(D, "KR", "005930"), "40_risk_check_real-main")
    assert rc["data"]["mode"] == "off" and isinstance(rc["data"]["approved"], bool)
    assert read_checkpoint(run_dir(D, "KR", "005930"), "50_order_real-main") is None
    assert ectx.brokers["real-main"].orders == []
    rows = ectx.conn.execute("SELECT account_id FROM orders").fetchall()
    assert all(r["account_id"] != "real-main" for r in rows)


def test_real_auto_requires_control_confirmation_record(ectx):
    real = ectx.accounts[1]
    control.set_flag(ectx.conn, "mode:real-main", "auto")  # 확인 문자열 없이 직접 쓴 값
    results = _run(ectx)
    r = next(x for x in results if x["account_id"] == "real-main")
    assert r["mode"] == "off" and "확인" in r["note"] and ectx.brokers["real-main"].orders == []
    control.set_account_mode(ectx.conn, real, "auto", confirm="LIVE")
    assert control.get_flag(ectx.conn, "live_confirmed:real-main")
    results = _run(ectx, ok_decision(ticker="000660"))
    r = next(x for x in results if x["account_id"] == "real-main")
    assert r["mode"] == "auto" and len(ectx.brokers["real-main"].orders) == 1
    control.set_account_mode(ectx.conn, real, "off")  # auto 해제 시 확인 기록도 지운다
    assert not control.get_flag(ectx.conn, "live_confirmed:real-main")


def test_approve_mode_creates_approval_and_notifies(ectx):
    control.set_account_mode(ectx.conn, ectx.accounts[0], "approve")
    results = _run(ectx)
    r = next(x for x in results if x["account_id"] == "paper-main")
    assert r["mode"] == "approve" and r["approval_id"] is not None and r["order"] is None
    pending = repo.pending_approvals(ectx.conn)
    assert len(pending) == 1 and pending[0]["order"]["qty"] == 13
    assert pending[0]["expires_at"] == "2026-10-09T01:30:00+00:00"
    assert ectx.brokers["paper-main"].orders == []
    assert any("/approve" in m for m in ectx.notifier.sent)


def test_dry_run_no_orders(ectx):
    results = _run(ectx, dry_run=True)
    r = next(x for x in results if x["account_id"] == "paper-main")
    assert r["approved"] and r["dry_run"] and r["order"] is None
    assert ectx.brokers["paper-main"].orders == [] and repo.pending_approvals(ectx.conn) == []
    rc = read_checkpoint(run_dir(D, "KR", "005930"), "40_risk_check_paper-main")
    assert rc["data"]["dry_run"] is True
    assert read_checkpoint(run_dir(D, "KR", "005930"), "50_order_paper-main") is None


def test_broker_failure_recorded_and_continues(ectx):
    second = AccountConfig(id="paper-2", env="paper", cano="3", markets=["KR"], mode="auto")
    ectx.accounts.append(second)
    ectx.brokers["paper-main"] = _broker(ectx.accounts[0], fail={"place_order"})
    ectx.brokers["paper-2"] = _broker(second)
    results = _run(ectx)
    by = {r["account_id"]: r for r in results}
    assert (
        by["paper-main"]["order"]["accepted"] is False
        and "가짜 실패" in by["paper-main"]["order"]["message"]
    )
    assert by["paper-2"]["order"]["accepted"] is True
    oc = read_checkpoint(run_dir(D, "KR", "005930"), "50_order_paper-main")
    assert oc["data"]["result"]["accepted"] is False
    rows = {r["account_id"]: r["status"] for r in ectx.conn.execute("SELECT * FROM orders")}
    assert rows == {"paper-main": "failed", "paper-2": "accepted"}


def test_balance_failure_rejects_account(ectx):
    ectx.brokers["paper-main"] = _broker(ectx.accounts[0], fail={"get_cash"})
    results = _run(ectx)
    r = next(x for x in results if x["account_id"] == "paper-main")
    assert r["approved"] is False and any("잔고" in s for s in r["reasons"])
    assert ectx.brokers["paper-main"].orders == []


def test_missing_broker_in_auto_is_rejected(ectx):
    """브로커가 없으면 잔고를 알 수 없으므로 판정 자체가 거부로 기록된다 (주문 없음)."""
    del ectx.brokers["paper-main"]
    results = _run(ectx)
    r = next(x for x in results if x["account_id"] == "paper-main")
    assert r["approved"] is False and r["order"] is None
    assert any("브로커" in s for s in r["reasons"])


def test_today_pnl_from_previous_snapshot_blocks_buy(ectx):
    repo.snapshot_positions(
        ectx.conn,
        account_id="paper-main",
        d=date(2026, 10, 8),
        nav=Decimal("10800000"),
        cash=Decimal("10800000"),
        positions=[],
    )
    assert repo.latest_nav_before(ectx.conn, "paper-main", D) == Decimal("10800000")
    results = _run(ectx)
    r = next(x for x in results if x["account_id"] == "paper-main")
    assert r["approved"] is False and any("일일 손실" in s for s in r["reasons"])


def test_us_order_carries_exchange(ectx):
    results = _run(
        ectx,
        ok_decision(ticker="AAPL", limit_price=200.0, take_profit=230.0),
        market="US",
        exchange="NAS",
        price=Decimal("200"),
    )
    r = next(x for x in results if x["account_id"] == "paper-main")
    assert r["approved"]
    o = ectx.brokers["paper-main"].orders[0]
    assert o.market == "US" and o.exchange == "NAS" and o.order_type == "LIMIT"
