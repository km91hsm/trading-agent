"""Phase 26 단계 2 Task 2.7: 성과·뷰·로트·메모리·준비도가 파생 계좌를 `asset` 로 가른다."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from trader import readiness
from trader.config import AccountConfig, Settings
from trader.fo import store
from trader.ledger import db, lots, performance, repo, views
from trader.memory import store as memory_store

ACCT = "paper-fo"
T = datetime(2026, 10, 27, 7, 0, tzinfo=UTC)


def fo_account() -> AccountConfig:
    return AccountConfig(
        id=ACCT, env="paper", cano="00000000", acnt_prdt_cd="03", markets=["KR"], mode="auto",
        asset="fo",
    )  # fmt: skip


def _navs(conn, pairs):
    with store.tx(conn):
        for d, v in pairs:
            store.write_nav_snapshot(
                conn, account_id=ACCT, date=d, nav=Decimal(v), cash=Decimal(v),
                margin_total=Decimal(0), add_margin=Decimal(0),
            )  # fmt: skip


def test_nav_series_fo_adapter(conn):
    # 10-23 금 · 10-24 토(제외, 현물 nav_history 와 같은 거래일 표본) · 10-27 화 · 10-28(nav 없음)
    _navs(conn, [("2026-10-23", "100"), ("2026-10-24", "101"), ("2026-10-27", "102")])
    with store.tx(conn):
        store.write_nav_snapshot(
            conn, account_id=ACCT, date="2026-10-28", nav=None, cash=None, margin_total=None,
            add_margin=None,
        )  # fmt: skip
    s = performance.nav_series(conn, ACCT, "KR", days=30, asset="fo")
    assert s == [("2026-10-23", Decimal("100")), ("2026-10-27", Decimal("102"))]
    assert performance.nav_series(conn, ACCT, "KR", days=30) == []  # 현물 경로엔 없다
    rep = performance.report(conn, None, ACCT, "KR", days=30, asset="fo")
    assert rep["metrics"]["return"] == 0.02 and rep["metrics"]["days"] == 2


def test_report_fo_empty_when_no_snapshot(conn):
    rep = performance.report(conn, None, ACCT, "KR", days=30, asset="fo")
    assert rep["metrics"]["return"] is None


def test_lots_fo_empty(conn):
    """파생 체결은 로트(FIFO·세금) 대상이 아니다 — ValueError 가 아니라 빈 결과."""
    oid = repo.insert_order(
        conn, decision_id=None, account_id=ACCT, ticker="105W11", market="KR", side="BUY",
        qty=2, order_type="LIMIT", price=Decimal("350"), status="filled", order_no="A",
        message="", mode="auto", asset="fo",
    )  # fmt: skip
    repo.insert_fill(conn, order_id=oid, account_id=ACCT, qty=2, price=Decimal("350"))
    assert lots.load_fills(conn, ACCT, "KR") == []


def test_account_summary_and_asset(conn):
    db.sync_accounts(conn, [fo_account()])
    assert repo.account_asset(conn, ACCT) == "fo"
    assert repo.account_asset(conn, "paper-main") == "stock"
    _navs(conn, [("2026-10-27", "100")])
    s = views.account_summary(conn, ACCT, date(2026, 10, 27))
    assert s == {
        "asset": "fo", "stance_by_market": {}, "nav_by_market": {"KR": "100"},
        "pending_approvals": 0,
    }  # fmt: skip
    assert views.account_summary(conn, "paper-main", date(2026, 10, 27))["asset"] == "stock"


def test_memory_held_at_none_for_fo(conn):
    assert (
        memory_store.held_at(conn, account_id=ACCT, market="KR", ticker="105W11", d="2026-10-27")
        is None
    )


def test_readiness_skips_fo(conn, project: Path):
    _navs(conn, [("2026-10-23", "100"), ("2026-10-27", "90")])
    out = readiness.check(
        conn, accounts=[fo_account()], settings=Settings(), state=project / "state",
        today=date(2026, 10, 27),
    )  # fmt: skip
    items = {i["key"]: i for i in out["items"]}
    # 파생 NAV −10% 가 준비도에 섞이지 않는다(값 없음 = 평가 대상 계좌 없음)
    assert items["return_vs_benchmark"]["value"] is None
    assert items["mdd"]["value"] is None
