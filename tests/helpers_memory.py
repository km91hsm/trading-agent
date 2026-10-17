"""메모리·보정 테스트용: 만기 결과가 채워진 결정 n건을 만든다 (애널리스트 신호 포함)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from helpers_agents import ok_decision, ok_report

from trader.ledger import repo
from trader.memory import store

ROLES = ("technical", "fundamental", "news_macro", "sentiment")


def seed_matured(
    conn,
    n: int,
    *,
    hit_rate: float = 0.7,
    market: str = "KR",
    start: date | None = None,
    vec=None,
    now: datetime | None = None,
    action: str = "BUY",
) -> list[int]:
    """i 번째 결정: 신호 BUY, 가격수익은 hit_rate 비율로 양수(적중) 아니면 음수. HOLD 는 실현 0."""
    now = now or datetime(2026, 10, 17, tzinfo=UTC)
    start = start or date(2026, 1, 5)
    ids = []
    for i in range(n):
        d = start + timedelta(days=i)
        ticker = f"T{i % 7:03d}"
        dec_id = repo.ingest_checkpoint(
            conn, d, market, ticker, "30_decision", {"data": ok_decision(ticker=ticker)}
        )
        for role in ROLES:
            sig = "BUY" if role != "sentiment" else "SELL"
            repo.ingest_checkpoint(
                conn,
                d,
                market,
                ticker,
                f"10_analyst_{role}",
                {"data": ok_report(role=role, signal=sig)},
            )
        eid = store.record_decision(
            conn,
            decision_id=dec_id,
            ticker=ticker,
            market=market,
            account_id="paper-main",
            decided_at=datetime(d.year, d.month, d.day, 6, tzinfo=UTC).isoformat(),
            decision={"action": action, "confidence": 0.6},
            row=None,
            horizon_days=5,
            vector=vec or [0.5] * 12,
        )
        hit = (i % 10) < int(round(hit_rate * 10))
        price = 0.03 if hit else -0.03
        ret = 0.0 if action == "HOLD" else price
        store.fill_outcome(
            conn,
            eid,
            realized_return=ret,
            alpha=ret - 0.001,
            outcome_at=(now - timedelta(days=1)).isoformat(),
            price_return=price,
        )
        ids.append(eid)
    return ids
