"""트리거 재평가 (스펙 §5.2): trader 재호출 세션(kind=resume) → 31_decision_r<n> → 같은 게이트."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from trader.config import AccountConfig, Settings
from trader.ledger import repo
from trader.orchestrator import session as session_mod

logger = logging.getLogger("trader.monitor")


def reevaluate(
    settings: Settings,
    conn: sqlite3.Connection,
    accounts: list[AccountConfig],
    *,
    market: str,
    date: date,
    ticker: str,
    exchange: str | None,
    trigger_id: int,
    trigger: dict,
    brokers: dict[str, Any] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    dry_run: bool = False,
    state: Path | None = None,
    session_runner: Callable[..., Any] | None = None,
    notifier: Any = None,
) -> session_mod.SessionResult:
    """재평가 세션 1회. 결정 제출·실행은 세션 안의 `tool submit-decision --stage` 가 한다."""
    spec = session_mod.SessionSpec(
        market=market,
        date=date,
        tickers=[{"ticker": ticker, "exchange": exchange}],
        dry_run=dry_run,
        asof=now(),
        kind="resume",
        trigger=trigger,
    )
    runner = session_runner or session_mod.run_session
    res = runner(
        spec,
        settings,
        conn,
        accounts,
        state=state,
        now=now,
        brokers=brokers or {},
        notifier=notifier,
    )
    if trigger_id:
        repo.mark_trigger_handled(
            conn, trigger_id, handled_marker(res), handled_at=now().isoformat()
        )
    if not res.ok:
        logger.warning("재평가 세션 실패 %s %s: %s", market, ticker, res.error)
    return res


def handled_marker(res: session_mod.SessionResult) -> str | None:
    """triggers.session_id 에 남길 값. 세션이 떴어도 실패(타임아웃·예산·오류)면 '실패:' 접두사를
    붙여 운영일 1회 제한(`repo.reevaluated_between`)에서 빼고 30분 뒤 재시도할 수 있게 한다."""
    if res.ok:
        return res.session_id
    return f"실패: {res.error or 'unknown'} ({res.session_id or '-'})"[:200]
