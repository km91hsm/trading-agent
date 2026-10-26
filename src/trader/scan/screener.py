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
from trader.platform import paths

logger = logging.getLogger("trader.scan")

PICKS = 5
PICKS_ON_CAP = 3
PICKS_ON_HARD_CAP = 1


def screen_context(conn: sqlite3.Connection, market: str, date: date) -> dict:
    """스크리너 보조 입력(Phase 21): 국면(state·count) + 오늘 시장 세션 제안(stance·확신·요약).
    없으면 None 값. 피처·뉴스처럼 '사실' 이 아니라 '시장 톤' 이라 screener.md 가 가중만 한다."""
    from trader.orchestrator.tools import market_view_today
    from trader.risk import regime as regime_mod

    reg = regime_mod.load(conn, market) or {}
    view = market_view_today(conn, market, date)
    return {
        "regime": (
            {"state": reg.get("state"), "count": reg.get("count")} if reg.get("state") else None
        ),
        "market_view": (
            {
                k: view.get(k)
                for k in (
                    "stance",
                    "confidence",
                    "hedge_weight",
                    "rationale",
                    "invalidation_conditions",
                )
                if k in view
            }
            if view
            else None
        ),
    }


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
    cancel: Any | None = None,
    universe: Any | None = None,
    discovery: bool = False,
    sources_cfg: Any | None = None,
    http: Any | None = None,
) -> dict:
    """{picks, analyze_targets, session, discovery}. analyze_targets = picks + 보유종목 + watch
    (중복 제거). 헤지 ETF(국면 규칙이 사고판다)는 분석 대상이 아니고, US 보유의 거래소는 잔고 응답
    (`Position.exchange`) → 후보 → 발굴(`_discovery`) → universe.us 순으로 찾는다.
    discovery=True 면 세션 앞에서 scout 입력(`_discovery_inputs`)을 만들어 세션 안의 발굴 단계가
    돌 수 있게 한다(세션 수 불변). 실패는 경고만 — 발굴 없이 스크리닝한다. `sources_cfg`·`http`
    가 있으면 발굴 입력에 네이버 블로그 최근 글도 싣는다. 유니버스 밖 발굴 종목은 분석 대상에
    `discovery: "listed"` 를 달아 데몬이 피처 스냅샷을 먼저 찍는다."""
    session_result = None
    held_tickers: list[str] = []
    if brokers:
        for a in accounts:
            if market in a.markets and a.id in brokers:
                try:
                    held_tickers += [p.ticker for p in brokers[a.id].get_positions(market)]
                except Exception as e:  # noqa: BLE001
                    logger.debug("보유 조회 실패(발굴 입력) %s: %s", a.id, e)
    mdir = run_store.market_dir(date, market, state)
    if discovery and not cap_hit and candidates and universe is not None:
        try:
            from trader import names
            from trader.config import load_sources
            from trader.scan import discovery as discovery_mod

            try:
                news_cfg = load_sources().news
            except Exception:  # noqa: BLE001
                news_cfg = None
            cache_dir = (state or paths.state_dir()) / "cache"
            discovery_mod.build_inputs(
                conn,
                settings,
                universe,
                market=market,
                today=date,
                asof=asof or now(),
                quant_candidates=candidates,
                held=held_tickers,
                state=state,
                now=now,
                news_cfg=news_cfg,
                cache_names=names.load_cache_names(cache_dir),
                sources_cfg=sources_cfg,
                http=http,
            )
        except Exception as e:  # noqa: BLE001 - 발굴 입력 실패는 스크리닝을 막지 않는다
            logger.warning("발굴 입력 조립 실패 %s: %s", market, e)
    if cap_hit or not candidates:
        n = PICKS_ON_CAP if candidates else 0
        scored = [c for c in candidates if c.get("score") is not None]  # 피처 없는 종목 제외
        picks = [{"ticker": c["ticker"], "reason": "세션 상한: 정량 상위"} for c in scored[:n]]
    else:
        spec = session_mod.SessionSpec(
            market=market,
            date=date,
            tickers=[],
            dry_run=True,
            asof=asof,
            kind="screen",
            candidates=candidates[:30],
            context=screen_context(conn, market, date),
        )
        session_result = session_mod.run_session(
            spec,
            settings,
            conn,
            accounts,
            state=state,
            now=now,
            brokers=brokers or {},
            cancel=cancel,
        )
        screened = read_checkpoint(run_store.market_dir(date, market, state), "_screen") or {}
        picks = list((screened.get("data") or {}).get("picks") or [])
        if not session_result.ok or not picks:
            logger.warning("screener 세션 실패 또는 빈 picks → 정량 상위 %d", PICKS_ON_CAP)
            scored = [c for c in candidates if c.get("score") is not None]
            picks = [
                {"ticker": c["ticker"], "reason": "screener 실패: 정량 상위"}
                for c in scored[:PICKS_ON_CAP]
            ]
    exch = {c["ticker"]: c.get("exchange") for c in candidates}
    discovered = (read_checkpoint(mdir, "_discovery") or {}).get("data") if discovery else None
    listed_tier: set[str] = set()
    for c in (discovered or {}).get("candidates") or []:
        exch.setdefault(c["ticker"], c.get("exchange"))
        if c.get("tier") == "listed":
            listed_tier.add(c["ticker"])
    if universe is not None and market == "US":
        for u in getattr(universe, "us", []) or []:
            exch.setdefault(u.symbol, u.exchange)
    hedge_tickers = settings.excluded_tickers(market)  # 헤지 ETF + 현금 단기금리 ETF
    targets: list[dict] = []
    seen: set[str] = set()

    def add(ticker: str, exchange: str | None, source: str = "pick") -> None:
        if ticker not in seen:
            seen.add(ticker)
            t: dict = {"ticker": ticker, "exchange": exchange, "source": source}
            if ticker in listed_tier:
                t["discovery"] = "listed"  # 유니버스 밖: 피처 스토어에 행이 없다
            targets.append(t)

    for p in picks:
        add(p["ticker"], exch.get(p["ticker"]))
    for a in accounts:
        if market not in a.markets or not brokers or a.id not in brokers:
            continue
        try:
            for pos in brokers[a.id].get_positions(market):
                if pos.ticker in hedge_tickers:
                    continue  # 헤지 ETF 는 국면 규칙만 사고판다
                add(pos.ticker, getattr(pos, "exchange", None) or exch.get(pos.ticker), "held")
        except Exception as e:  # noqa: BLE001 - 보유 조회 실패는 후보만으로 진행
            logger.warning("보유종목 조회 실패 %s: %s", a.id, e)
    for w in watch or []:
        wm, wt = split_market(str(w).strip().upper())
        if wm == market:  # 다른 시장의 관심종목은 이 시장 분석 대상이 아니다
            add(wt, exch.get(wt), "watch")
    from trader.scan import discovery as discovery_mod

    return {
        "picks": picks,
        "analyze_targets": targets,
        "discovery": discovery_mod.summary(discovered) if discovery else None,
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
