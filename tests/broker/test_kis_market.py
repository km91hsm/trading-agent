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


# --- Phase 2: 분봉·호가·투자자동향·지수 ------------------------------------

DOM_MINUTE = "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
DOM_ORDERBOOK = "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn"
DOM_INVESTOR = "/uapi/domestic-stock/v1/quotations/inquire-investor"
DOM_INDEX = "/uapi/domestic-stock/v1/quotations/inquire-index-price"
OVS_MINUTE = "/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice"
OVS_ORDERBOOK = "/uapi/overseas-price/v1/quotations/inquire-asking-price"


def test_minute_kr_ascending_with_time(market, respx_mock):
    route = respx_mock.get(REAL + DOM_MINUTE).mock(return_value=resp("minute_kr.json"))
    candles = market.get_minute_kr("005930")
    assert [c.time for c in candles] == ["152800", "152900", "153000"]
    assert candles[-1].close == Decimal("71500") and candles[-1].volume == 12000
    assert candles[0].date == date(2026, 10, 8)
    h = route.calls[0].request.headers
    assert h["tr_id"] == "FHKST03010200"
    p = route.calls[0].request.url.params
    assert p["FID_INPUT_ISCD"] == "005930" and p["FID_PW_DATA_INCU_YN"] == "N"


def test_orderbook_kr(market, respx_mock):
    respx_mock.get(REAL + DOM_ORDERBOOK).mock(return_value=resp("orderbook_kr.json"))
    ob = market.get_orderbook_kr("005930")
    assert ob.market == "KR" and len(ob.asks) == 10 and len(ob.bids) == 10
    assert ob.asks[0] == (Decimal("71600"), 100) and ob.bids[0] == (Decimal("71500"), 110)
    assert ob.asks[9] == (Decimal("72500"), 1000) and ob.asof.tzinfo is not None


def test_investor_kr_ascending(market, respx_mock):
    route = respx_mock.get(REAL + DOM_INVESTOR).mock(return_value=resp("investor_kr.json"))
    flows = market.get_investor_kr("005930", days=2)
    assert [f.date for f in flows] == [date(2026, 10, 7), date(2026, 10, 8)]
    assert flows[-1].foreign_net == 8000 and flows[-1].individual_net == -12000
    assert flows[-1].institution_net == 4000
    assert route.calls[0].request.headers["tr_id"] == "FHKST01010900"


def test_index_kr(market, respx_mock):
    route = respx_mock.get(REAL + DOM_INDEX).mock(return_value=resp("index_kr.json"))
    q = market.get_index_kr()
    assert q.ticker == "0001" and q.price == Decimal("3250.12")
    assert q.change_pct == pytest.approx(0.0038) and q.volume == 456789000
    p = route.calls[0].request.url.params
    assert p["FID_COND_MRKT_DIV_CODE"] == "U" and p["FID_INPUT_ISCD"] == "0001"


def test_minute_us_ascending(market, respx_mock):
    route = respx_mock.get(REAL + OVS_MINUTE).mock(return_value=resp("minute_us.json"))
    candles = market.get_minute_us("AAPL", "NAS")
    assert [c.time for c in candles] == ["155800", "155900"]
    assert candles[-1].close == Decimal("227.5500") and candles[-1].volume == 120000
    p = route.calls[0].request.url.params
    assert p["EXCD"] == "NAS" and p["SYMB"] == "AAPL" and p["NMIN"] == "1"


def test_orderbook_us_drops_empty_levels(market, respx_mock):
    respx_mock.get(REAL + OVS_ORDERBOOK).mock(return_value=resp("orderbook_us.json"))
    ob = market.get_orderbook_us("AAPL", "NAS")
    assert ob.market == "US" and len(ob.asks) == 3 and len(ob.bids) == 3
    assert ob.asks[0] == (Decimal("227.5600"), 300) and ob.bids[0] == (Decimal("227.5500"), 250)
