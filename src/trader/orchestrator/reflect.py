"""복기 세션 (스펙 §6.5): 결과가 채워졌지만 반성이 없는 엔트리를 reflector 에게 넘긴다.

세션은 만기 엔트리가 있을 때만 띄운다(LLM 비용). FAKE_CLAUDE=1 이면 NEUTRAL 반성.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

from trader.config import KST, AccountConfig, Settings
from trader.orchestrator import session as session_mod

logger = logging.getLogger("trader.memory")


def pending(conn: sqlite3.Connection, *, market: str | None = None, limit: int = 20) -> list[dict]:
    """반성이 없는 만기 엔트리 + 당시 애널리스트 신호 {role: signal}."""
    sql = "SELECT * FROM memory_entries WHERE outcome_at IS NOT NULL AND reflection IS NULL"
    args: list = []
    if market:
        sql += " AND market=?"
        args.append(market)
    out = []
    for r in conn.execute(sql + " ORDER BY outcome_at LIMIT ?", [*args, limit]).fetchall():
        try:
            day = datetime.fromisoformat(r["decided_at"]).astimezone(KST).date().isoformat()
        except ValueError:
            day = None
        signals = {}
        if day:
            for a in conn.execute(
                "SELECT role, signal FROM analyst_reports WHERE date=? AND market=? AND ticker=?",
                (day, r["market"], r["ticker"]),
            ).fetchall():
                signals[a["role"]] = a["signal"]
        out.append(
            {
                "entry_id": int(r["id"]),
                "ticker": r["ticker"],
                "market": r["market"],
                "decided_at": r["decided_at"],
                "decision": json.loads(r["decision"] or "{}"),
                "situation_summary": r["situation_summary"],
                "horizon_days": r["horizon_days"],
                "realized_return": r["realized_return"],
                "alpha_vs_benchmark": r["alpha_vs_benchmark"],
                "analyst_signals": signals,
            }
        )
    return out


def run_reflect(
    settings: Settings,
    conn: sqlite3.Connection,
    accounts: list[AccountConfig],
    *,
    market: str,
    date: date,
    state: Path | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    dry_run: bool = True,
    brokers: dict | None = None,
    notifier=None,
) -> session_mod.SessionResult:
    items = pending(conn, market=market)
    if not items:
        return session_mod.SessionResult(ok=True, session_id=None, stages_written=[])
    spec = session_mod.SessionSpec(
        market=market,
        date=date,
        tickers=[],
        dry_run=dry_run,
        asof=now(),
        kind="reflect",
        entries=items,
    )
    return session_mod.run_session(
        spec,
        settings,
        conn,
        accounts,
        state=state,
        now=now,
        brokers=brokers or {},
        notifier=notifier,
    )
