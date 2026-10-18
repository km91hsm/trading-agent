"""원장 집계 (스펙 §6.3 일별 집계 중 Phase 4 몫): 결정 분포·수렴율·미승인 주문·계좌 성향."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta

from trader.config import KST
from trader.ledger import repo
from trader.orchestrator.summary import CONVERGENCE_GAP
from trader.risk import stance as stance_mod

_SIGN = {"BULLISH": 1.0, "NEUTRAL": 0.0, "BEARISH": -1.0}


def _since(days: int, now: datetime | None) -> str:
    """decisions.date 는 KST 날짜이므로 기준일도 KST 로 잡는다."""
    now = now or datetime.now(UTC)
    return (now.astimezone(KST).date() - timedelta(days=days)).isoformat()


def decision_distribution(
    conn: sqlite3.Connection,
    days: int = 30,
    *,
    now: datetime | None = None,
    market: str | None = None,
) -> dict[str, int]:
    """최근 `days` 일 결정의 action 별 건수 (0건인 action 은 생략)."""
    sql = "SELECT action, COUNT(*) AS n FROM decisions WHERE date >= ?"
    args: list = [_since(days, now)]
    if market:
        sql += " AND market = ?"
        args.append(market)
    rows = conn.execute(sql + " GROUP BY action ORDER BY action", args).fetchall()
    return {r["action"]: int(r["n"]) for r in rows}


def convergence_rate(
    conn: sqlite3.Connection, days: int = 30, *, now: datetime | None = None
) -> float | None:
    """최종 라운드에서 양측 부호 확신 차이가 CONVERGENCE_GAP 이하인 종목 비율 (토론 없으면 None)."""
    rows = conn.execute(
        "SELECT date, market, ticker, side, round, stance, conviction FROM debate_turns "
        "WHERE date >= ?",
        (_since(days, now),),
    ).fetchall()
    by_key: dict[tuple, dict[int, dict[str, float]]] = {}
    for r in rows:
        signed = float(r["conviction"] or 0.0) * _SIGN.get(r["stance"] or "", 0.0)
        by_key.setdefault((r["date"], r["market"], r["ticker"]), {}).setdefault(
            int(r["round"]), {}
        )[r["side"]] = signed
    total = converged = 0
    for rounds in by_key.values():
        final = rounds[max(rounds)]
        if "bull" not in final or "bear" not in final:
            continue
        total += 1
        if abs(final["bull"] - final["bear"]) <= CONVERGENCE_GAP:
            converged += 1
    return converged / total if total else None


def pending_approvals(conn: sqlite3.Connection) -> list[dict]:
    return repo.pending_approvals(conn)


def account_stance(
    conn: sqlite3.Connection, account_id: str, today: date, *, market: str | None = None
) -> str:
    """계좌 성향. market 을 주면 executor 와 같은 기준(그 시장의 최근 5거래일 실현손익)이다."""
    return stance_mod.decide(repo.realized_pnl_5d(conn, account_id, today, market=market))


def stance_by_market(
    conn: sqlite3.Connection, account_id: str, today: date, markets: Iterable[str]
) -> dict[str, str]:
    """시장별 성향. 통화가 달라 KR·US 실현손익을 합산하지 않는다(executor 판정과 동일)."""
    return {m: account_stance(conn, account_id, today, market=m) for m in markets}
