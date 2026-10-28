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
APPROVAL_PATH = "/oauth2/Approval"  # 실시간 웹소켓 접속키
# 실시간 웹소켓(시세는 실전 env 만 쓴다 — 모의 서버는 시세를 주지 않는다)
WS_URL: dict[Env, str] = {
    "real": "ws://ops.koreainvestment.com:21000",
    "paper": "ws://ops.koreainvestment.com:31000",
}

# 이름 -> (경로, 실전 tr_id, 모의 tr_id | None)
_TR: dict[str, tuple[str, str, str | None]] = {
    # 예탁원 권리 일정(기준일) — Stock 프로젝트가 공식 샘플과 대조한 값(D-108, 2026-10). 시세 env
    "KSD_PAIDIN": (
        "/uapi/domestic-stock/v1/ksdinfo/paidin-capin",
        "HHKDB669100C0",
        "HHKDB669100C0",
    ),
    "KSD_BONUS": (
        "/uapi/domestic-stock/v1/ksdinfo/bonus-issue",
        "HHKDB669101C0",
        "HHKDB669101C0",
    ),
    "KSD_MERGER_SPLIT": (
        "/uapi/domestic-stock/v1/ksdinfo/merger-split",
        "HHKDB669104C0",
        "HHKDB669104C0",
    ),
    "KSD_REV_SPLIT": (
        "/uapi/domestic-stock/v1/ksdinfo/rev-split",
        "HHKDB669105C0",
        "HHKDB669105C0",
    ),
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
    "DOM_MINUTE": (
        "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
        "FHKST03010200",
        "FHKST03010200",
    ),
    "DOM_ORDERBOOK": (
        "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
        "FHKST01010200",
        "FHKST01010200",
    ),
    "DOM_INVESTOR": (
        "/uapi/domestic-stock/v1/quotations/inquire-investor",
        "FHKST01010900",
        "FHKST01010900",
    ),
    "DOM_INDEX": (
        "/uapi/domestic-stock/v1/quotations/inquire-index-price",
        "FHPUP02100000",
        "FHPUP02100000",
    ),
    "HOLIDAY": ("/uapi/domestic-stock/v1/quotations/chk-holiday", "CTCA0903R", None),
    # 국내 순위 (Phase 25 테마 열기, 실전 전용 — 응답 필드명 미확인, architecture.md 「미확인」)
    "VOLUME_RANK": ("/uapi/domestic-stock/v1/quotations/volume-rank", "FHPST01710000", None),
    "FLUCT_RANK": ("/uapi/domestic-stock/v1/ranking/fluctuation", "FHPST01700000", None),
    "FRGN_INST_TOTAL": (
        "/uapi/domestic-stock/v1/quotations/foreign-institution-total",
        "FHPTJ04400000",
        None,
    ),
    # 국내 계좌
    "DOM_BALANCE": ("/uapi/domestic-stock/v1/trading/inquire-balance", "TTTC8434R", "VTTC8434R"),
    "DOM_ORDER_BUY": ("/uapi/domestic-stock/v1/trading/order-cash", "TTTC0012U", "VTTC0012U"),
    "DOM_ORDER_SELL": ("/uapi/domestic-stock/v1/trading/order-cash", "TTTC0011U", "VTTC0011U"),
    "DOM_CCLD": (  # 일별 주문체결 (3개월 이내)
        "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
        "TTTC0081R",
        "VTTC0081R",
    ),
    "DOM_ORDER_CANCEL": (
        "/uapi/domestic-stock/v1/trading/order-rvsecncl",
        "TTTC0013U",
        "VTTC0013U",
    ),
    # 해외 시세
    "OVS_PRICE": ("/uapi/overseas-price/v1/quotations/price", "HHDFS00000300", "HHDFS00000300"),
    # 종목 기본정보(이름) — 데몬 names_fill 만 쓴다(화면 표시 경로는 네트워크 없음).
    # 2026-10-29 실기 확인
    "DOM_STOCK_INFO": ("/uapi/domestic-stock/v1/quotations/search-stock-info", "CTPF1002R", None),
    "OVS_STOCK_INFO": ("/uapi/overseas-price/v1/quotations/search-info", "CTPF1702R", None),
    # 현재가상세: 업종코드(e_icod) — 섹터 제안(universe sectors)에만 쓴다
    "OVS_PRICE_DETAIL": (
        "/uapi/overseas-price/v1/quotations/price-detail",
        "HHDFS76200200",
        "HHDFS76200200",
    ),
    "OVS_DAILY": (
        "/uapi/overseas-price/v1/quotations/dailyprice",
        "HHDFS76240000",
        "HHDFS76240000",
    ),
    "OVS_MINUTE": (
        "/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice",
        "HHDFS76950200",
        "HHDFS76950200",
    ),
    "OVS_ORDERBOOK": (
        "/uapi/overseas-price/v1/quotations/inquire-asking-price",
        "HHDFS76200100",
        "HHDFS76200100",
    ),
    # 해외 계좌
    "OVS_BALANCE": ("/uapi/overseas-stock/v1/trading/inquire-balance", "TTTS3012R", "VTTS3012R"),
    "OVS_PRESENT_BALANCE": (
        "/uapi/overseas-stock/v1/trading/inquire-present-balance",
        "CTRP6504R",
        "VTRP6504R",
    ),
    "OVS_ORDER_BUY_US": ("/uapi/overseas-stock/v1/trading/order", "TTTT1002U", "VTTT1002U"),
    "OVS_ORDER_SELL_US": ("/uapi/overseas-stock/v1/trading/order", "TTTT1006U", "VTTT1006U"),
    "OVS_CCLD_US": (  # 해외 주문체결 내역
        "/uapi/overseas-stock/v1/trading/inquire-ccnl",
        "TTTS3035R",
        "VTTS3035R",
    ),
    "OVS_ORDER_CANCEL_US": (
        "/uapi/overseas-stock/v1/trading/order-rvsecncl",
        "TTTT1004U",
        "VTTT1004U",
    ),
    # --- 국내선물옵션(Phase 26). examples_llm/domestic_futureoption/* 2026-10-27 확인.
    # 시세는 실전·모의 같은 tr_id, 주문·계좌는 모의 주간만, 야간(*_NIGHT)은 모의 미지원(None) ---
    "FO_PRICE": (  # 선물옵션 시세(옵션은 델타·감마·세타·베가·IV·행사가 포함)
        "/uapi/domestic-futureoption/v1/quotations/inquire-price",
        "FHMIF10000000",
        "FHMIF10000000",
    ),
    "FO_ORDERBOOK": (
        "/uapi/domestic-futureoption/v1/quotations/inquire-asking-price",
        "FHMIF10010000",
        "FHMIF10010000",
    ),
    "FO_DAILY": (
        "/uapi/domestic-futureoption/v1/quotations/inquire-daily-fuopchartprice",
        "FHKIF03020100",
        "FHKIF03020100",
    ),
    "FO_MINUTE": (
        "/uapi/domestic-futureoption/v1/quotations/inquire-time-fuopchartprice",
        "FHKIF03020200",
        "FHKIF03020200",
    ),
    "FO_BALANCE": (  # 선물옵션 잔고현황(선물·옵션 같은 TR, CTX_AREA_*200 연속조회)
        "/uapi/domestic-futureoption/v1/trading/inquire-balance",
        "CTFO6118R",
        "VTFO6118R",
    ),
    "FO_ORDER": ("/uapi/domestic-futureoption/v1/trading/order", "TTTO1101U", "VTTO1101U"),
    "FO_ORDER_CANCEL": (
        "/uapi/domestic-futureoption/v1/trading/order-rvsecncl",
        "TTTO1103U",
        "VTTO1103U",
    ),
    "FO_CCLD": (  # 주문체결내역(qty 는 잔량, tot_ccld_qty 는 누적 체결)
        "/uapi/domestic-futureoption/v1/trading/inquire-ccnl",
        "TTTO5201R",
        "VTTO5201R",
    ),
    "FO_PSBL_ORDER": (
        "/uapi/domestic-futureoption/v1/trading/inquire-psbl-order",
        "TTTO5105R",
        "VTTO5105R",
    ),
    "FO_ORDER_NIGHT": ("/uapi/domestic-futureoption/v1/trading/order", "STTN1101U", None),
    "FO_ORDER_CANCEL_NIGHT": (
        "/uapi/domestic-futureoption/v1/trading/order-rvsecncl",
        "TTTN1103U",
        None,
    ),
    "FO_CCLD_NIGHT": (  # 야간 체결 tr_id 는 미확인(실전 전환 Phase 에서 확정)
        "/uapi/domestic-futureoption/v1/trading/inquire-ccnl",
        "STTN5201R",
        None,
    ),
    "FO_BALANCE_NIGHT": (
        "/uapi/domestic-futureoption/v1/trading/inquire-balance",
        "CTFN6118R",
        None,
    ),
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
