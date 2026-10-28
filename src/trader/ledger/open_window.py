"""열린 창 종목(전체 검토 2차 2-L1): 아직 결과가 확정되지 않은 종목 — 보유 중, 만기 전 메모리
엔트리, 만기 전 섀도 포트폴리오 비중 — 을 모아 일일 피처 스냅샷 대상에 **유니버스에 더해** 넣는다.

유니버스 밖(scout `listed`·`event`) LLM 매수 종목은 분석 직전 1회 스냅샷뿐이라 만기 봉이 생기지
않았고, `shadow.mature` 가 종가 없는 종목을 빼고 재정규화해 "유니버스 안 선택" 만 채점하는 선택
편향이 있었다. US 는 거래소 코드가 있어야 일봉을 받을 수 있어 관측값(`repo.ticker_exchanges`,
잔고·00_inputs 에서 적는다)이 없으면 경고하고 뺀다(다음 관측 뒤 다시 들어온다).
"""

from __future__ import annotations

import logging
import sqlite3

from trader.ledger import repo, shadow
from trader.memory import store

logger = logging.getLogger("trader.ledger")


def tickers(conn: sqlite3.Connection, market: str) -> set[str]:
    """보유(최근 스냅샷 수량 > 0) ∪ 미만기 메모리 엔트리 ∪ 미만기 섀도 비중(지수 proxy 제외)."""
    return (
        repo.held_tickers_latest(conn, market)
        | store.pending_tickers(conn, market)
        | shadow.pending_tickers(conn, market)
    )


def targets(conn: sqlite3.Connection, market: str, universe) -> list[tuple[str, str, str | None]]:
    """유니버스 밖 열린 창 종목의 스냅샷 대상 `[(코드, 시장, 거래소)]` (코드 순). 유니버스 종목은
    일일 스냅샷이 이미 찍으므로 뺀다. 거래소를 모르는 US 종목은 경고 후 제외."""
    if market == "US":
        known = {s.symbol for s in getattr(universe, "us", None) or []}
        exchanges = repo.ticker_exchanges(conn, market)
    else:
        known = set(getattr(universe, "kr", None) or [])
        exchanges = {}
    out: list[tuple[str, str, str | None]] = []
    for t in sorted(tickers(conn, market) - known):
        if market == "US":
            ex = exchanges.get(t)
            if not ex:
                logger.warning("열린 창 종목 %s %s: 거래소 미관측 — 스냅샷 생략", market, t)
                continue
            out.append((t, market, ex))
        else:
            out.append((t, market, None))
    return out
