"""screener 세션 (haiku): 후보 요약을 주고 picks 5개를 받는다. 세션 상한이면 정량 상위로 대신."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from trader.config import AccountConfig, Settings
from trader.control.control import split_market
from trader.orchestrator import run_store
from trader.orchestrator import session as session_mod
from trader.orchestrator.run_store import read_checkpoint

logger = logging.getLogger("trader.scan")

PICKS = 5
PICKS_ON_CAP = 3
PICKS_ON_HARD_CAP = 1


def run_screen(
    settings: Settings,
    conn: sqlite3.Connection,
    accounts: list[AccountConfig],
    *,
    market: str,
    date: date,
    candidates: list[dict],
    cap_hit: bool,
    state: Path | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    brokers: dict[str, Any] | None = None,
    watch: list[str] | None = None,
    asof: datetime | None = None,
) -> dict:
    """{picks, analyze_targets, session}. analyze_targets = picks + 보유종목 + watch (중복 제거)."""
    session_result = None
    if cap_hit or not candidates:
        n = PICKS_ON_CAP if candidates else 0
        picks = [{"ticker": c["ticker"], "reason": "세션 상한: 정량 상위"} for c in candidates[:n]]
    else:
        spec = session_mod.SessionSpec(
            market=market,
            date=date,
            tickers=[],
            dry_run=True,
            asof=asof,
            kind="screen",
            candidates=candidates[:30],
        )
        session_result = session_mod.run_session(
            spec, settings, conn, accounts, state=state, now=now, brokers=brokers or {}
        )
        screened = read_checkpoint(run_store.market_dir(date, market, state), "_screen") or {}
        picks = list((screened.get("data") or {}).get("picks") or [])
        if not session_result.ok or not picks:
            logger.warning("screener 세션 실패 또는 빈 picks → 정량 상위 %d", PICKS_ON_CAP)
            picks = [
                {"ticker": c["ticker"], "reason": "screener 실패: 정량 상위"}
                for c in candidates[:PICKS_ON_CAP]
            ]
    exch = {c["ticker"]: c.get("exchange") for c in candidates}
    targets: list[dict] = []
    seen: set[str] = set()

    def add(ticker: str, exchange: str | None) -> None:
        if ticker not in seen:
            seen.add(ticker)
            targets.append({"ticker": ticker, "exchange": exchange})

    for p in picks:
        add(p["ticker"], exch.get(p["ticker"]))
    for a in accounts:
        if market not in a.markets or not brokers or a.id not in brokers:
            continue
        try:
            for pos in brokers[a.id].get_positions(market):
                add(pos.ticker, exch.get(pos.ticker))
        except Exception as e:  # noqa: BLE001 - 보유 조회 실패는 후보만으로 진행
            logger.warning("보유종목 조회 실패 %s: %s", a.id, e)
    for w in watch or []:
        wm, wt = split_market(str(w).strip().upper())
        if wm == market:  # 다른 시장의 관심종목은 이 시장 분석 대상이 아니다
            add(wt, exch.get(wt))
    return {
        "picks": picks,
        "analyze_targets": targets,
        "session": (
            {
                "ok": session_result.ok,
                "fake": session_result.fake,
                "cost_usd": session_result.cost_usd,
            }
            if session_result
            else None
        ),
    }
