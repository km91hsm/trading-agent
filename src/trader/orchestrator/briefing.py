"""아침 브리핑(Stock docs/06 「07:30 브리핑」): 밤사이 미국장 지수 변화·국면·시장 세션 제안·밤사이
뉴스·오늘 일정·보유 종목을 haiku 단발 호출(`quick.ask_json`, 에이전트 `briefing`)로 5줄 요약해
텔레그램으로 보낸다. **사람이 읽는 요약 전용** — 결정·게이트·주문은 이 결과를 읽지 않는다.
구독 사용률이 soft 이상이면(`usage.session_allowance(kind="briefing")` 가 resume 만 허용하는 구간)
건너뛴다. 하루 상한 `briefing.daily_cap`(분석 세션 상한과 별도, `agent_calls.agent='briefing'`)."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from trader.agents import schemas
from trader.config import Settings
from trader.ledger import repo, views
from trader.notify import messages

logger = logging.getLogger("trader.orchestrator")

NEWS_HOURS = 12
NEWS_LIMIT = 15


def build_inputs(
    conn: sqlite3.Connection,
    settings: Settings,
    accounts: list,
    *,
    now: datetime,
    market_client: Any = None,
    saveticker_root: Path | None = None,
) -> dict:
    from trader.config import KST
    from trader.ledger import shadow
    from trader.orchestrator import market_view
    from trader.orchestrator.run_store import run_date_for
    from trader.risk import regime
    from trader.sources import news_store
    from trader.sources import saveticker as st

    kst_today = now.astimezone(KST).date()  # 07:20 KST 는 UTC 로 아직 전날이다(Codex 리뷰)
    us = None
    idx = shadow.index_table(market_client, settings.hedge, "US", days=5, now=now)
    if not idx.empty and len(idx) >= 2:
        sym = str(idx.columns[0])
        c0, c1 = float(idx[sym].iloc[-2]), float(idx[sym].iloc[-1])
        us = {"symbol": sym, "close": c1, "change_pct": round(c1 / c0 - 1, 4) if c0 else None}
    views_by_market = {}
    for m in ("KR", "US"):
        v = market_view.load_view(conn, m, run_date_for(m, now)) or None
        views_by_market[m] = (
            {k: v.get(k) for k in ("stance", "confidence", "rationale")} if v else None
        )
    news = {
        m: [
            {k: n.get(k) for k in ("title", "source", "published_at", "tickers")}
            for n in news_store.recent(conn, m, asof=now, hours=NEWS_HOURS, limit=NEWS_LIMIT)
        ]
        for m in ("US", "KR")
    }
    holdings = []
    known = repo.ticker_names(conn)
    for a in accounts:
        for m in a.markets:
            snap = views.latest_positions(conn, a.id, m) or {}
            nav = float(snap.get("nav") or 0)
            for p in snap.get("positions") or []:
                if float(p.get("eval_amount") or 0) <= 0 or nav <= 0:
                    continue
                holdings.append(
                    {
                        "market": m,
                        "ticker": p["ticker"],
                        "name": known.get((m, p["ticker"])),
                        "weight": round(float(p["eval_amount"]) / nav, 4),
                    }
                )
    calendar = st.upcoming(saveticker_root, asof=now, days=2) if saveticker_root else []
    return {
        "date": kst_today.isoformat(),
        "us_overnight": us,
        "regime": regime.summary(conn, ["KR", "US"], today=kst_today),
        "market_view": views_by_market,
        "news": news,
        "calendar": calendar[:10],
        "holdings": holdings,
    }


def run(
    conn: sqlite3.Connection,
    settings: Settings,
    accounts: list,
    *,
    now: datetime,
    notifier: Any,
    market_client: Any = None,
    saveticker_root: Path | None = None,
    runner: Any = None,
    cancel: Any = None,
) -> dict:
    from trader import usage
    from trader.orchestrator import quick

    cfg = settings.briefing
    if not cfg.enabled:
        return {"skipped": "꺼짐"}
    ok, why = usage.session_allowance(conn, settings, kind="briefing", now=now)
    if not ok:
        return {"skipped": f"감속: {why}"}
    payload = build_inputs(
        conn,
        settings,
        accounts,
        now=now,
        market_client=market_client,
        saveticker_root=saveticker_root,
    )
    got = quick.ask_json(
        settings,
        conn,
        agent="briefing",
        payload=payload,
        schema=schemas.Briefing,
        cap=cfg.daily_cap,
        timeout_s=cfg.timeout_s,
        runner=runner,
        now=lambda: now,
        cancel=cancel,
    )
    if got is None:
        return {"skipped": "판정 없음(상한·실패)"}
    notifier.send(messages.briefing(got.lines, got.uncertain))
    return {"lines": len(got.lines)}
