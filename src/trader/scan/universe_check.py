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
# 재실행하면 사라질 수 있는 KIS 오류 (초당 한도·네트워크·서버 5xx).
# 그 밖의 코드는 종목 쪽 문제로 본다.
TRANSIENT_CODES = {"EGW00201", "NETWORK", "NETWORK_UNKNOWN", "AUTH"}


def _transient(e: KisApiError) -> bool:
    code = str(getattr(e, "msg_cd", "") or "")
    return code in TRANSIENT_CODES or code.startswith("HTTP5")


def _price_us(market_client: Any, symbol: str, exchange: str) -> tuple[Decimal | None, str | None]:
    """(가격, 일시 오류 note). 일시 오류면 (None, note), 그 밖의 실패·가격 0 은 (None, None)."""
    try:
        q = market_client.get_price_us(symbol, exchange)
    except KisApiError as e:
        logger.info("시세 조회 실패 %s %s: %s", symbol, exchange, e)
        if _transient(e):
            code = getattr(e, "msg_cd", "")
            return None, f"일시 오류 가능: KisApiError {code} — 재실행으로 확인"
        return None, None
    except ValueError as e:
        logger.info("시세 조회 실패 %s %s: %s", symbol, exchange, e)
        return None, None
    return (q.price if q.price and q.price > 0 else None), None


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
            except KisApiError as e:
                # 한도 초과·네트워크 같은 일시 오류는 상장폐지·오코드와 다르다 → 재실행으로 가린다
                ok = False
                code = getattr(e, "msg_cd", "")
                note = (
                    f"일시 오류 가능: KisApiError {code} — 재실행으로 확인"
                    if _transient(e)
                    else f"가격 없음: KisApiError {code}"
                )
            except ValueError as e:
                ok, note = False, f"가격 없음: {type(e).__name__}"
            out.append(
                {"ticker": t, "exchange": None, "ok": ok, "suggested_exchange": None, "note": note}
            )
        return out
    for u in universe.us:
        price, transient = _price_us(market_client, u.symbol, u.exchange)
        if transient:
            # 한도 초과·네트워크면 다른 거래소를 더듬지 않는다 (잘못된 거래소 제안 방지)
            out.append(
                {
                    "ticker": u.symbol,
                    "exchange": u.exchange,
                    "ok": False,
                    "suggested_exchange": None,
                    "note": transient,
                }
            )
            continue
        if price is not None:
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
            (
                x
                for x in US_EXCHANGES
                if x != u.exchange and _price_us(market_client, u.symbol, x)[0] is not None
            ),
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
