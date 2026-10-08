"""KIS tr_id·경로의 유일한 출처.

값은 공식 open-trading-api examples_llm/ 에서 2026-10-09 확인했다.
모의 서버가 지원하지 않는 API 는 paper 값이 None 이다.
"""

from __future__ import annotations

from trader.broker.errors import KisApiError
from trader.config import Env

BASE_URL: dict[Env, str] = {
    "real": "https://openapi.koreainvestment.com:9443",
    "paper": "https://openapivts.koreainvestment.com:29443",
}

TOKEN_PATH = "/oauth2/tokenP"

# 이름 -> (경로, 실전 tr_id, 모의 tr_id | None)
_TR: dict[str, tuple[str, str, str | None]] = {
    # 국내 시세 (실전 env 로만 호출한다)
    "DOM_PRICE": (
        "/uapi/domestic-stock/v1/quotations/inquire-price",
        "FHKST01010100",
        "FHKST01010100",
    ),
    "DOM_DAILY": (
        "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
        "FHKST03010100",
        "FHKST03010100",
    ),
    "HOLIDAY": ("/uapi/domestic-stock/v1/quotations/chk-holiday", "CTCA0903R", None),
    # 국내 계좌
    "DOM_BALANCE": ("/uapi/domestic-stock/v1/trading/inquire-balance", "TTTC8434R", "VTTC8434R"),
    "DOM_PSBL_ORDER": (
        "/uapi/domestic-stock/v1/trading/inquire-psbl-order",
        "TTTC8908R",
        "VTTC8908R",
    ),
    "DOM_ORDER_BUY": ("/uapi/domestic-stock/v1/trading/order-cash", "TTTC0012U", "VTTC0012U"),
    "DOM_ORDER_SELL": ("/uapi/domestic-stock/v1/trading/order-cash", "TTTC0011U", "VTTC0011U"),
    # 해외 시세
    "OVS_PRICE": ("/uapi/overseas-price/v1/quotations/price", "HHDFS00000300", "HHDFS00000300"),
    "OVS_DAILY": (
        "/uapi/overseas-price/v1/quotations/dailyprice",
        "HHDFS76240000",
        "HHDFS76240000",
    ),
    # 해외 계좌
    "OVS_BALANCE": ("/uapi/overseas-stock/v1/trading/inquire-balance", "TTTS3012R", "VTTS3012R"),
    "OVS_PSBL_ORDER": (
        "/uapi/overseas-stock/v1/trading/inquire-psamount",
        "TTTS3007R",
        "VTTS3007R",
    ),
    "OVS_ORDER_BUY_US": ("/uapi/overseas-stock/v1/trading/order", "TTTT1002U", "VTTT1002U"),
    "OVS_ORDER_SELL_US": ("/uapi/overseas-stock/v1/trading/order", "TTTT1006U", "VTTT1006U"),
}


def path(name: str) -> str:
    return _TR[name][0]


def tr_id(name: str, env: Env) -> str:
    """이름과 env 로 tr_id 를 고른다. 모의 미지원이면 KisApiError(UNSUPPORTED)."""
    _, real, paper = _TR[name]
    if env == "real":
        return real
    if paper is None:
        raise KisApiError("UNSUPPORTED", f"모의 서버가 지원하지 않는 API 입니다: {name}", real)
    return paper


def names() -> list[str]:
    return list(_TR)
