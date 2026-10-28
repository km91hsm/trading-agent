"""종목명 조회(KIS 종목 기본정보). 화면 표시 경로는 부르지 않고 데몬 `names_fill` 만 쓴다.
응답은 구조만 본뜬 합성 값."""

import httpx

from trader.broker.kis_market import KisMarket

REAL = "https://openapi.koreainvestment.com:9443"
DOM_INFO = "/uapi/domestic-stock/v1/quotations/search-stock-info"
OVS_INFO = "/uapi/overseas-price/v1/quotations/search-info"


def ok(output):
    return httpx.Response(200, json={"rt_cd": "0", "msg_cd": "MCA00000", "msg1": "ok",
                                     "output": output})  # fmt: skip


def test_kr_name_uses_short_product_name(real_client, respx_mock):
    route = respx_mock.get(REAL + DOM_INFO).mock(
        return_value=ok({"prdt_abrv_name": "합성ETF 인버스2X", "prdt_name": "합성 긴 이름"})
    )
    assert KisMarket(real_client).get_stock_name("KR", "123450") == "합성ETF 인버스2X"
    p = route.calls[0].request.url.params
    assert p["PRDT_TYPE_CD"] == "300" and p["PDNO"] == "123450"


def test_us_name_tries_exchanges_until_found(real_client, respx_mock):
    seen = []

    def respond(request):
        code = request.url.params["PRDT_TYPE_CD"]
        seen.append(code)
        return ok({"prdt_name": "합성 미국 ETF"} if code == "529" else {"prdt_name": ""})

    respx_mock.get(REAL + OVS_INFO).mock(side_effect=respond)
    assert KisMarket(real_client).get_stock_name("US", "ZZZ") == "합성 미국 ETF"
    assert seen == ["512", "513", "529"]  # 거래소를 모르면 NASDAQ → NYSE → AMEX
    seen.clear()
    assert KisMarket(real_client).get_stock_name("US", "ZZZ", exchange="AMS") == "합성 미국 ETF"
    assert seen == ["529"]


def test_name_missing_or_error_is_none(real_client, respx_mock):
    respx_mock.get(REAL + DOM_INFO).mock(
        return_value=httpx.Response(200, json={"rt_cd": "1", "msg_cd": "X", "msg1": "없음"})
    )
    assert KisMarket(real_client).get_stock_name("KR", "000000") is None
