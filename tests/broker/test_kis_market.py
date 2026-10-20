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


def test_name_kr_from_daily_output1(market, respx_mock):
    respx_mock.get(REAL + DOM_DAILY).mock(return_value=resp("daily_kr_p1.json"))
    assert market.get_name_kr("005930") == "삼성전자"


# --- 분봉 역방향 페이징 (분봉 차트) ------------------------------------------


def _minute_payload(times: list[str], d: str = "20261008") -> dict:
    rows = [
        {
            "stck_bsop_date": d,
            "stck_cntg_hour": t,
            "stck_prpr": "71500",
            "stck_oprc": "71400",
            "stck_hgpr": "71600",
            "stck_lwpr": "71300",
            "cntg_vol": "100",
            "acml_tr_pbmn": "1",
        }
        for t in times
    ]
    return {"rt_cd": "0", "msg_cd": "MCA00000", "msg1": "ok", "output1": {}, "output2": rows}


def _hhmm_range(end: str, n: int) -> list[str]:
    """end(HHMMSS) 부터 1분씩 내려가며 n 개 (KIS 처럼 내림차순)."""
    from datetime import datetime, timedelta

    t = datetime.strptime(end, "%H%M%S")
    return [(t - timedelta(minutes=i)).strftime("%H%M%S") for i in range(n)]


def test_minute_kr_pages_backward_and_stops(market, respx_mock):
    first = _minute_payload(_hhmm_range("153000", 30))  # 153000..150100
    second = _minute_payload(_hhmm_range("150000", 30))  # 150000..143100
    route = respx_mock.get(REAL + DOM_MINUTE).mock(
        side_effect=[
            httpx.Response(200, json=first),
            httpx.Response(200, json=second),
            httpx.Response(200, json=second),  # 같은 봉 반복 → 멈춰야 한다
        ]
    )
    candles = market.get_minute_kr("005930", count=70)
    assert len(route.calls) == 3
    assert route.calls[1].request.url.params["FID_INPUT_HOUR_1"] == "150000"
    assert route.calls[2].request.url.params["FID_INPUT_HOUR_1"] == "143000"
    assert len(candles) == 60 and candles[0].time == "143100" and candles[-1].time == "153000"
    assert len({c.time for c in candles}) == 60


def test_minute_kr_stops_at_open_and_short_page(market, respx_mock):
    route = respx_mock.get(REAL + DOM_MINUTE).mock(
        return_value=httpx.Response(200, json=_minute_payload(_hhmm_range("092000", 21)))
    )
    candles = market.get_minute_kr("005930", count=390)
    assert len(route.calls) == 1 and len(candles) == 21 and candles[0].time == "090000"


def test_minute_kr_default_count_single_call(market, respx_mock):
    route = respx_mock.get(REAL + DOM_MINUTE).mock(return_value=resp("minute_kr.json"))
    assert len(market.get_minute_kr("005930")) == 3 and len(route.calls) == 1


def test_fake_market_minutes():
    from trader.broker.fake import FakeMarket
    from trader.broker.models import Candle

    one = Decimal(1)
    c = [
        Candle(
            date=date(2026, 10, 8),
            open=one,
            high=one,
            low=one,
            close=one,
            volume=1,
            time=f"09{i:02d}00",
        )
        for i in range(5)
    ]
    fm = FakeMarket(minutes={"005930": c})
    assert [x.time for x in fm.get_minute_kr("005930", count=2)] == ["090300", "090400"]
    assert fm.get_minute_us("AAPL", "NAS") == [] and fm.calls == ["get_minute_kr", "get_minute_us"]


def test_minute_kr_caps_pages_on_abnormal_responses(market, respx_mock):
    """매 페이지가 30건이지만 새 봉이 1건뿐인 비정상 응답이면 호출 수를 상한에서 멈춘다."""
    base = _hhmm_range("153000", 29)  # 매번 반복되는 29건

    def reply(request):
        k = len(route.calls)
        new = _hhmm_range("150000", 1 + k)[-1]  # 호출마다 1분씩 더 이른 새 봉 1건
        return httpx.Response(200, json=_minute_payload([*base, new]))

    route = respx_mock.get(REAL + DOM_MINUTE).mock(side_effect=reply)
    candles = market.get_minute_kr("005930", count=390)
    assert len(route.calls) == market.MINUTE_MAX_PAGES < 390
    assert len(candles) == 29 + market.MINUTE_MAX_PAGES


def test_minute_kr_drops_rows_without_time(market, respx_mock):
    payload = _minute_payload(["153000", "", "152800"])
    respx_mock.get(REAL + DOM_MINUTE).mock(return_value=httpx.Response(200, json=payload))
    candles = market.get_minute_kr("005930")
    assert [c.time for c in candles] == ["152800", "153000"]


def test_minute_kr_drops_previous_day_rows_and_stops(market, respx_mock):
    """개장 전 시각으로 페이징하면 KIS 가 전일 봉을 섞어 준다. 당일 봉만 남기고 멈춘다."""
    first = _minute_payload(_hhmm_range("093000", 30))  # 093000..090100
    mixed = {
        **_minute_payload(["090000"]),
        "output2": _minute_payload(["090000"])["output2"]
        + _minute_payload(_hhmm_range("153000", 29), d="20261007")["output2"],
    }
    route = respx_mock.get(REAL + DOM_MINUTE).mock(
        side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=mixed)]
    )
    candles = market.get_minute_kr("005930", count=390)
    assert len(route.calls) == 2
    assert {c.date for c in candles} == {date(2026, 10, 8)}
    assert len(candles) == 31 and candles[0].time == "090000"


def test_daily_skips_rows_with_blank_date(market, respx_mock):
    """상장 100일 미만 종목은 KIS 가 뒤를 빈 행으로 채운다 → 전체 실패가 아니라 그 행만 버린다."""
    payload = load("daily_kr_p1.json")
    payload["output2"] = payload["output2"][:10] + [
        {k: "" for k in payload["output2"][0]},
        {"stck_bsop_date": None},
    ]
    respx_mock.get(REAL + DOM_DAILY).mock(return_value=httpx.Response(200, json=payload))
    candles = market.get_daily_kr("005930", days=50)
    assert len(candles) == 10 and candles[-1].date == date(2026, 10, 9)
    us = load("daily_us.json")
    us["output2"] = us["output2"][:5] + [{k: "" for k in us["output2"][0]}]
    respx_mock.get(REAL + OVS_DAILY).mock(return_value=httpx.Response(200, json=us))
    assert len(market.get_daily_us("AAPL", "NAS", days=30)) == 5


def test_minute_kr_blank_time_row_does_not_stop_paging(market, respx_mock):
    """30행 중 하나가 빈 시각이어도(버림) 페이지가 '짧다' 고 보고 멈추면 안 된다."""
    times = _hhmm_range("153000", 30)
    times[5] = ""
    first = _minute_payload(times)
    second = _minute_payload(_hhmm_range("150000", 10))
    route = respx_mock.get(REAL + DOM_MINUTE).mock(
        side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=second)]
    )
    candles = market.get_minute_kr("005930", count=390)
    assert len(route.calls) == 2 and len(candles) == 39


OVS_PRICE_DETAIL = "/uapi/overseas-price/v1/quotations/price-detail"


def test_industry_kr_and_us(market, respx_mock):
    """섹터 제안(universe sectors)용 업종: KR 은 현재가 응답의 업종한글명, US 는 현재가상세 e_icod."""
    respx_mock.get(REAL + DOM_PRICE).mock(return_value=resp("price_kr.json"))
    assert market.get_industry_kr("005930") == "전기전자"
    route = respx_mock.get(REAL + OVS_PRICE_DETAIL).mock(return_value=resp("price_detail_us.json"))
    assert market.get_industry_us("AAPL", "NAS") == "730"
    p = route.calls[0].request.url.params
    assert p["EXCD"] == "NAS" and p["SYMB"] == "AAPL"
    assert route.calls[0].request.headers["tr_id"] == "HHDFS76200200"
    respx_mock.get(REAL + DOM_PRICE).mock(
        return_value=httpx.Response(200, json={"rt_cd": "0", "output": {"stck_prpr": "1"}})
    )
    assert market.get_industry_kr("000660") is None  # 업종 없음 → None
