"""유니버스 점검: KIS 가 실제로 시세를 주는 코드만 남기고, US 는 데이터가 오는 거래소 코드를 알린다.

유니버스에 틀린 코드·거래소가 있으면 수집·백필이 조용히 빈 결과를 내므로(실기: WMT 는 NAS 로만 옴)
확대 전에 한 번 돌려 걸러낸다. 호출은 종목당 1~4회(KIS 실전 20/s).
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from trader.broker.errors import KisApiError
from trader.config import Universe

logger = logging.getLogger("trader.scan")

US_EXCHANGES = ("NAS", "NYS", "AMS")


def _price_us(market_client: Any, symbol: str, exchange: str) -> Decimal | None:
    try:
        q = market_client.get_price_us(symbol, exchange)
    except (KisApiError, ValueError) as e:
        logger.info("시세 조회 실패 %s %s: %s", symbol, exchange, e)
        return None
    return q.price if q.price and q.price > 0 else None


def check(universe: Universe, market_client: Any, *, market: str) -> list[dict]:
    """종목마다 {ticker, exchange, ok, suggested_exchange, note}. US 는 선언 거래소로 가격이 없으면
    NAS·NYS·AMS 순으로 다시 조회해 데이터가 오는 코드를 suggested_exchange 에 적는다."""
    out: list[dict] = []
    if market == "KR":
        for t in universe.kr:
            try:
                q = market_client.get_price_kr(t)
                ok = bool(q.price and q.price > 0)
                note = "" if ok else "가격 0"
            except (KisApiError, ValueError) as e:
                ok, note = False, f"가격 없음: {type(e).__name__}"
            out.append(
                {"ticker": t, "exchange": None, "ok": ok, "suggested_exchange": None, "note": note}
            )
        return out
    for u in universe.us:
        if _price_us(market_client, u.symbol, u.exchange) is not None:
            out.append(
                {
                    "ticker": u.symbol,
                    "exchange": u.exchange,
                    "ok": True,
                    "suggested_exchange": None,
                    "note": "",
                }
            )
            continue
        found = next(
            (x for x in US_EXCHANGES if x != u.exchange and _price_us(market_client, u.symbol, x)),
            None,
        )
        out.append(
            {
                "ticker": u.symbol,
                "exchange": u.exchange,
                "ok": False,
                "suggested_exchange": found,
                "note": f"{u.exchange} 로 가격 없음"
                + (f" → {found} 로 바꾸세요" if found else ", 어느 거래소도 없음"),
            }
        )
    return out
