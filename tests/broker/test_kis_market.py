from datetime import date
from decimal import Decimal

import httpx
import pytest
from conftest_broker import load

from trader.broker.kis_market import KisMarket

REAL = "https://openapi.koreainvestment.com:9443"
DOM_PRICE = "/uapi/domestic-stock/v1/quotations/inquire-price"
DOM_DAILY = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
OVS_PRICE = "/uapi/overseas-price/v1/quotations/price"
OVS_DAILY = "/uapi/overseas-price/v1/quotations/dailyprice"
HOLIDAY = "/uapi/domestic-stock/v1/quotations/chk-holiday"


def resp(name):
    return httpx.Response(200, json=load(name))


@pytest.fixture
def market(real_client):
    return KisMarket(real_client)


def test_paper_env_rejected(paper_client):
    with pytest.raises(ValueError, match="실전"):
        KisMarket(paper_client)


def test_price_kr(market, respx_mock):
    route = respx_mock.get(REAL + DOM_PRICE).mock(return_value=resp("price_kr.json"))
    q = market.get_price_kr("005930")
    assert q.price == Decimal("71500") and q.change_pct == pytest.approx(0.007)
    assert q.volume == 12345678 and q.market == "KR" and q.asof.tzinfo is not None
    p = route.calls[0].request.url.params
    assert p["FID_COND_MRKT_DIV_CODE"] == "J" and p["FID_INPUT_ISCD"] == "005930"


def test_price_us(market, respx_mock):
    route = respx_mock.get(REAL + OVS_PRICE).mock(return_value=resp("price_us.json"))
    q = market.get_price_us("AAPL", "NAS")
    assert q.price == Decimal("227.5500") and q.change_pct == pytest.approx(0.0108)
    assert q.volume == 38000000 and q.market == "US"
    p = route.calls[0].request.url.params
    assert p["EXCD"] == "NAS" and p["SYMB"] == "AAPL" and p["AUTH"] == ""


def test_daily_paging_ascending_unique(market, respx_mock):
    route = respx_mock.get(REAL + DOM_DAILY).mock(
        side_effect=[resp("daily_kr_p1.json"), resp("daily_kr_p2.json")]
    )
    candles = market.get_daily_kr("005930", days=120)
    assert len(candles) == 120  # 경계 중복 1건 제거
    dates = [c.date for c in candles]
    assert dates == sorted(dates) and len(set(dates)) == 120
    assert candles[-1].date == date(2026, 10, 9) and candles[-1].close == Decimal("70000")
    assert isinstance(candles[0].open, Decimal) and candles[0].time is None
    p1 = route.calls[0].request.url.params
    assert p1["FID_PERIOD_DIV_CODE"] == "D" and p1["FID_ORG_ADJ_PRC"] == "0"
    p2 = route.calls[1].request.url.params
    assert p2["FID_INPUT_DATE_2"] < p1["FID_INPUT_DATE_2"]  # 2페이지는 더 과거


def test_daily_kr_single_page_when_enough(market, respx_mock):
    route = respx_mock.get(REAL + DOM_DAILY).mock(return_value=resp("daily_kr_p1.json"))
    candles = market.get_daily_kr("005930", days=50)
    assert len(candles) == 50 and route.call_count == 1


def test_daily_us(market, respx_mock):
    route = respx_mock.get(REAL + OVS_DAILY).mock(return_value=resp("daily_us.json"))
    candles = market.get_daily_us("AAPL", "NAS", days=30)
    assert len(candles) == 30 and candles[-1].date == date(2026, 10, 9)
    assert candles[-1].close == Decimal("220.0000") and candles[0].date < candles[-1].date
    p = route.calls[0].request.url.params
    assert p["GUBN"] == "0" and p["MODP"] == "1"


def test_holiday_cached(market, respx_mock, project):
    route = respx_mock.get(REAL + HOLIDAY).mock(return_value=resp("holiday.json"))
    assert market.is_holiday(date(2026, 10, 10)) is True
    assert market.is_holiday(date(2026, 10, 12)) is False
    assert market.is_holiday(date(2026, 10, 9)) is True  # opnd_yn=N
    assert route.call_count == 1
    assert (project / "state" / "cache" / "holiday_202610.json").exists()
    assert route.calls[0].request.url.params["BASS_DT"] == "20261001"


def test_holiday_unknown_date_not_holiday(market, respx_mock):
    respx_mock.get(REAL + HOLIDAY).mock(return_value=resp("holiday.json"))
    assert market.is_holiday(date(2026, 10, 30)) is False
