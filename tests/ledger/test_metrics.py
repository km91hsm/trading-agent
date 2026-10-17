from datetime import UTC, date, datetime
from decimal import Decimal

from helpers_agents import ok_decision, ok_turn

from trader.ledger import metrics, repo

D = date(2026, 10, 9)
NOW = datetime(2026, 10, 9, 6, 0, tzinfo=UTC)


def _decision(conn, ticker, action, d=D):
    repo.ingest_checkpoint(
        conn, d, "KR", ticker, "30_decision", {"data": ok_decision(ticker=ticker, action=action)}
    )


def test_decision_distribution(conn):
    _decision(conn, "005930", "BUY")
    _decision(conn, "000660", "HOLD")
    _decision(conn, "035420", "HOLD")
    _decision(conn, "005380", "SELL", d=date(2026, 8, 1))  # 30일 밖
    assert metrics.decision_distribution(conn, days=30, now=NOW) == {"BUY": 1, "HOLD": 2}
    assert metrics.decision_distribution(conn, days=365, now=NOW)["SELL"] == 1
    assert metrics.decision_distribution(conn, days=30, now=NOW, market="US") == {}


def test_convergence_rate(conn):
    assert metrics.convergence_rate(conn, days=30, now=NOW) is None
    # 005930: 최종 라운드 양측 부호 확신 차이 ≤ 0.2 → 수렴
    for side, stance, conv in (("bull", "BULLISH", 0.6), ("bear", "BULLISH", 0.5)):
        repo.ingest_checkpoint(
            conn,
            D,
            "KR",
            "005930",
            f"20_debate_r1_{side}",
            {"data": ok_turn(side, round=1, stance=stance, conviction=conv)},
        )
    # 000660: 라운드 0 만, 대립 → 비수렴
    for side, stance in (("bull", "BULLISH"), ("bear", "BEARISH")):
        repo.ingest_checkpoint(
            conn,
            D,
            "KR",
            "000660",
            f"20_debate_r0_{side}",
            {"data": ok_turn(side, round=0, stance=stance, conviction=0.7)},
        )
    assert metrics.convergence_rate(conn, days=30, now=NOW) == 0.5


def test_pending_and_stances(conn):
    _decision(conn, "005930", "BUY")
    did = repo.decision_id(conn, D, "KR", "005930")
    repo.create_approval(
        conn,
        decision_id=did,
        account_id="paper-main",
        order={"ticker": "005930", "qty": 1},
        requested_at=NOW.isoformat(),
        expires_at="2026-10-09T06:30:00+00:00",
    )
    assert [a["id"] for a in metrics.pending_approvals(conn)] == [1]
    assert metrics.account_stance(conn, "paper-main", D) == "normal"
    oid = repo.insert_order(
        conn,
        decision_id=did,
        account_id="paper-main",
        ticker="005930",
        market="KR",
        side="BUY",
        qty=1,
        order_type="LIMIT",
        price=Decimal("100"),
        status="accepted",
        order_no="1",
        message="",
        mode="auto",
        created_at="2026-10-07T01:00:00+00:00",
    )
    repo.insert_fill(
        conn,
        order_id=oid,
        account_id="paper-main",
        qty=1,
        price=Decimal("100"),
        filled_at="2026-10-07T01:00:00+00:00",
    )
    sid = repo.insert_order(
        conn,
        decision_id=did,
        account_id="paper-main",
        ticker="005930",
        market="KR",
        side="SELL",
        qty=1,
        order_type="LIMIT",
        price=Decimal("90"),
        status="accepted",
        order_no="2",
        message="",
        mode="auto",
        created_at="2026-10-08T01:00:00+00:00",
    )
    repo.insert_fill(
        conn,
        order_id=sid,
        account_id="paper-main",
        qty=1,
        price=Decimal("90"),
        filled_at="2026-10-08T01:00:00+00:00",
    )
    assert metrics.account_stance(conn, "paper-main", D) == "conservative"
    # KR 손실만 있다: KR 은 conservative, US 는 normal, 시장 없는 합산은 conservative
    assert metrics.account_stance(conn, "paper-main", D, market="KR") == "conservative"
    assert metrics.account_stance(conn, "paper-main", D, market="US") == "normal"
    assert metrics.stance_by_market(conn, "paper-main", D, ["KR", "US"]) == {
        "KR": "conservative",
        "US": "normal",
    }


def test_metrics_window_uses_kst_date(conn):
    """UTC 15:30 = KST 다음 날 00:30. 30일 창의 기준일은 KST 날짜여야 한다."""
    from trader.ledger import views

    _decision(conn, "005930", "BUY", d=date(2026, 10, 8))
    late = datetime(2026, 10, 9, 15, 30, tzinfo=UTC)  # KST 2026-10-10 00:30
    assert metrics.decision_distribution(conn, days=1, now=late) == {}  # since = 10-09 (KST)
    assert metrics.decision_distribution(conn, days=2, now=late) == {"BUY": 1}
    assert views.list_decisions(conn, days=1, now=late) == []
