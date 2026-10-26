"""Phase 25 단계 6: 승인 지문 — 요청 뒤 주문 내용이 바뀌면 실행하지 않는다."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from conftest import make_account
from helpers_agents import ok_decision

from trader.agents.schemas import Decision
from trader.broker.fake import FakeBroker
from trader.broker.models import Cash
from trader.config import Settings
from trader.ledger import repo
from trader.orchestrator import executor

T0 = datetime(2026, 10, 23, 1, 0, tzinfo=UTC)
D = date(2026, 10, 23)


class Sent:
    def __init__(self):
        self.sent = []

    def send(self, text, **kw):
        self.sent.append(text)
        return True


@pytest.fixture
def ectx(conn, project):
    acct = make_account("paper-main", "paper", "approve")
    broker = FakeBroker(
        acct,
        cash={"KR": Cash(currency="KRW", available=Decimal("10000000"), total=Decimal("10000000"))},
    )
    return executor.ExecContext(
        settings=Settings(),
        accounts=[acct],
        conn=conn,
        brokers={acct.id: broker},
        notifier=Sent(),
        now=lambda: T0,
    )


def _request(ectx, conn):
    dec = Decision(**ok_decision())
    repo.ingest_checkpoint(conn, D, "KR", "005930", "30_decision", {"data": dec.model_dump()})
    executor.execute(ectx, dec, date=D, market="KR", price=Decimal("71000"), dry_run=False)
    (a,) = repo.pending_approvals(conn)
    return a


def test_fingerprint_stable_key_order():
    a = executor.approval_fingerprint("acct", 3, {"ticker": "005930", "qty": 2, "price": "1"})
    b = executor.approval_fingerprint("acct", 3, {"price": "1", "qty": 2, "ticker": "005930"})
    assert a == b and len(a) == 64
    assert a != executor.approval_fingerprint(
        "acct", 3, {"ticker": "005930", "qty": 3, "price": "1"}
    )
    # 표시용 이름은 지문에 들어가지 않는다
    assert a == executor.approval_fingerprint(
        "acct", 3, {"ticker": "005930", "qty": 2, "price": "1", "name": "x"}
    )


def test_approve_rejects_tampered_qty(ectx, conn):
    a = _request(ectx, conn)
    row = conn.execute(
        "SELECT fingerprint, order_json FROM approvals WHERE id=?", (a["id"],)
    ).fetchone()
    assert row["fingerprint"]
    order = json.loads(row["order_json"])
    order["qty"] = int(order["qty"]) * 10
    conn.execute("UPDATE approvals SET order_json=? WHERE id=?", (json.dumps(order), a["id"]))
    conn.commit()
    r = executor.approve(ectx, a["id"])
    assert r["ok"] is False and "지문" in r["error"]
    assert ectx.brokers["paper-main"].orders == []
    assert repo.get_approval(conn, a["id"])["status"] == "failed"
    assert any("승인 지문 불일치" in m for m in ectx.notifier.sent)


def test_approve_legacy_null_fingerprint_passes(ectx, conn):
    a = _request(ectx, conn)
    conn.execute("UPDATE approvals SET fingerprint=NULL WHERE id=?", (a["id"],))
    conn.commit()
    r = executor.approve(ectx, a["id"])
    assert r["ok"] is True and len(ectx.brokers["paper-main"].orders) == 1


def test_approve_untampered_passes(ectx, conn):
    a = _request(ectx, conn)
    assert executor.approve(ectx, a["id"])["ok"] is True
