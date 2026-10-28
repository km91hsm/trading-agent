"""Phase 26 단계 1: 파생 시세·호가·일봉·분봉·체인·코스피200 시각."""

from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
from conftest_broker import load

from trader.broker import fo_master
from trader.broker.fake import FakeMarket
from trader.broker.kis_market import KisMarket
from trader.broker.models import FoQuote

REAL = "https://openapi.koreainvestment.com:9443"
BASE = "/uapi/domestic-futureoption/v1/quotations"
T0 = datetime(2026, 10, 27, 1, 15, 30, tzinfo=UTC)  # 10:15:30 KST


def no_book(respx_mock):
    """시세 시각·1호가를 채우는 호가 조회(실기 1)를 빈 응답으로 — 시세 필드만 보는 테스트용."""
    return respx_mock.get(REAL + BASE + "/inquire-asking-price").mock(
        return_value=httpx.Response(200, json={"rt_cd": "0", "output1": {}, "output2": {}})
    )


def test_fo_price_futures_fields(real_client, respx_mock):
    no_book(respx_mock)
    route = respx_mock.get(REAL + BASE + "/inquire-price").mock(
        return_value=httpx.Response(200, json=load("fo_price_futures.json"))
    )
    q = KisMarket(real_client, now=lambda: T0).get_fo_price("105W11", "FUTURES")
    p = route.calls[0].request.url.params
    assert p["FID_COND_MRKT_DIV_CODE"] == "F" and p["FID_INPUT_ISCD"] == "105W11"
    assert q.kind == "FUTURES" and q.price == Decimal("350.50") and q.basis == Decimal("0.70")
    assert round(q.change_pct, 6) == 0.0034 and q.volume == 12345 and q.open_interest == 55000
    assert q.upper == Decimal("380.0") and q.lower == Decimal("320.0")
    assert q.last_trade_date == date(2026, 11, 12) and q.days_left == 16
    assert q.hist_vol == 15.2 and q.iv is None and q.strike is None
    assert q.underlying == Decimal("349.80")
    assert q.quote_asof == datetime(2026, 10, 27, 1, 15, 30, tzinfo=UTC)
    assert q.received_at == T0


def test_fo_price_option_greeks(real_client, respx_mock):
    no_book(respx_mock)
    route = respx_mock.get(REAL + BASE + "/inquire-price").mock(
        return_value=httpx.Response(200, json=load("fo_price_option.json"))
    )
    q = KisMarket(real_client, now=lambda: T0).get_fo_price("205W11352", "CALL")
    assert route.calls[0].request.url.params["FID_COND_MRKT_DIV_CODE"] == "O"
    assert q.kind == "CALL" and q.price == Decimal("2.35") and q.strike == Decimal("352.50")
    assert q.delta == 0.42 and q.gamma == 0.03 and q.theta == -0.08 and q.vega == 0.25
    assert q.iv == 18.4 and q.hist_vol == 15.2 and q.basis is None
    assert q.quote_asof is None  # 시각 필드 없음 → None (received_at 로 대체하지 않음)


def test_fo_orderbook(real_client, respx_mock):
    respx_mock.get(REAL + BASE + "/inquire-asking-price").mock(
        return_value=httpx.Response(200, json=load("fo_orderbook.json"))
    )
    ob = KisMarket(real_client, now=lambda: T0).get_fo_orderbook("105W11", "FUTURES")
    assert ob.asks[0] == (Decimal("350.52"), 10) and ob.bids[0] == (Decimal("350.48"), 11)
    assert len(ob.asks) == 5 and ob.ticker == "105W11"


def test_fo_daily_ascending(real_client, respx_mock):
    route = respx_mock.get(REAL + BASE + "/inquire-daily-fuopchartprice").mock(
        return_value=httpx.Response(200, json=load("fo_daily.json"))
    )
    cs = KisMarket(real_client, now=lambda: T0).get_fo_daily("105W11", "FUTURES", days=100)
    p = route.calls[0].request.url.params
    assert p["FID_PERIOD_DIV_CODE"] == "D" and p["FID_INPUT_DATE_2"] == "20261027"
    assert [c.date for c in cs] == [date(2026, 10, 23), date(2026, 10, 26), date(2026, 10, 27)]
    assert cs[-1].close == Decimal("350.50") and cs[-1].volume == 12345


def test_fo_minute_skips_empty_time(real_client, respx_mock):
    route = respx_mock.get(REAL + BASE + "/inquire-time-fuopchartprice").mock(
        return_value=httpx.Response(200, json=load("fo_minute.json"))
    )
    cs = KisMarket(real_client, now=lambda: T0).get_fo_minute("105W11", "FUTURES", count=60)
    p = route.calls[0].request.url.params
    assert p["FID_HOUR_CLS_CODE"] == "60" and p["FID_INPUT_HOUR_1"] == "101530"
    assert [c.time for c in cs] == ["101400", "101500"]


def test_option_chain_skips_failed_rows(real_client, respx_mock):
    no_book(respx_mock)
    ok = load("fo_price_option.json")

    def respond(request):
        if request.url.params["FID_INPUT_ISCD"] == "205W11350":
            return httpx.Response(200, json={"rt_cd": "1", "msg_cd": "X", "msg1": "실패"})
        return httpx.Response(200, json=ok)

    route = respx_mock.get(REAL + BASE + "/inquire-price").mock(side_effect=respond)
    from pathlib import Path

    contracts = fo_master.parse(
        (Path(__file__).parent / "fixtures" / "fo_master_sample.mst").read_bytes()
    )
    chain = [
        c for c in contracts if c.expiry_ym == "202611" and c.kind != "FUTURES" and not c.weekly
    ]
    quotes, errors = KisMarket(real_client, now=lambda: T0).get_option_chain(
        chain, strikes=[Decimal("347.5"), Decimal("350.0"), Decimal("352.5")]
    )
    assert route.call_count == 6
    assert len(quotes) == 5 and errors == ["205W11350"]
    assert all(isinstance(q, FoQuote) for q in quotes)


def test_option_chain_caps_calls(real_client, respx_mock):
    no_book(respx_mock)
    respx_mock.get(REAL + BASE + "/inquire-price").mock(
        return_value=httpx.Response(200, json=load("fo_price_option.json"))
    )
    big = [
        fo_master.Contract(
            code=f"205W11{i:03d}", std_code="", name="", kind="CALL", weekly=False,
            strike=Decimal(i), expiry_ym="202611", underlying="K200", atm_flag=None,
            product_type="C",
        )
        for i in range(30)
    ]
    quotes, _ = KisMarket(real_client, now=lambda: T0).get_option_chain(
        big, strikes=[Decimal(i) for i in range(30)]
    )
    assert len(quotes) == 18


def test_kospi200_returns_time(real_client, respx_mock):
    respx_mock.get(REAL + "/uapi/domestic-stock/v1/quotations/inquire-index-price").mock(
        return_value=httpx.Response(200, json=load("fo_index_kr.json"))
    )
    px, asof = KisMarket(real_client, now=lambda: T0).get_kospi200()
    assert px == Decimal("349.80") and asof == datetime(2026, 10, 27, 1, 15, 30, tzinfo=UTC)
    data = load("fo_index_kr.json")
    del data["output"]["bsop_hour"]
    respx_mock.get(REAL + "/uapi/domestic-stock/v1/quotations/inquire-index-price").mock(
        return_value=httpx.Response(200, json=data)
    )
    px, asof = KisMarket(real_client, now=lambda: T0).get_kospi200()
    assert px == Decimal("349.80") and asof is None


def test_fake_market_fo_methods():
    q = FoQuote(
        code="105W11", kind="FUTURES", price=Decimal("350"), bid=None, ask=None, change_pct=0.0,
        volume=0, open_interest=None, basis=None, upper=None, lower=None, last_trade_date=None,
        days_left=None, hist_vol=None, iv=None, delta=None, gamma=None, theta=None, vega=None,
        strike=None, underlying=None, underlying_asof=None, quote_asof=None, received_at=T0,
    )
    m = FakeMarket(fo_quotes={"105W11": q}, kospi200=(Decimal("349.8"), T0), fail={"get_fo_daily"})
    assert m.get_fo_price("105W11", "FUTURES").price == Decimal("350")
    assert m.get_kospi200() == (Decimal("349.8"), T0)
    assert m.get_fo_minute("105W11", "FUTURES") == []
    import pytest

    from trader.broker.errors import KisApiError

    with pytest.raises(KisApiError):
        m.get_fo_daily("105W11", "FUTURES")
    with pytest.raises(KisApiError):
        m.get_fo_price("nope", "FUTURES")
    quotes, errors = m.get_option_chain([], strikes=[])
    assert quotes == [] and errors == []


def test_option_quote_iv_is_percent_and_reprice_converts(real_client, respx_mock):
    no_book(respx_mock)
    """Codex 단계 1 P0-3: 시세 IV 는 KIS 그대로 퍼센트(18.4)이고 Black-76 재평가는 /100 으로
    넣는다."""
    from trader.fo import pricing

    respx_mock.get(REAL + BASE + "/inquire-price").mock(
        return_value=httpx.Response(200, json=load("fo_price_option.json"))
    )
    q = KisMarket(real_client, now=lambda: T0).get_fo_price("205W11352", "CALL")
    assert q.iv == 18.4 and q.hist_vol == 15.2
    got = pricing.reprice(q, index_shift_pct=0.0, iv_rel=0.0, days_left=q.days_left)
    want = pricing.price("CALL", q.underlying, q.strike, 0.184, q.days_left)
    assert got == want and got < Decimal("50")


# 실기 1(2026-10-28): 시세(FHMIF10000000) 응답에는 시각·1호가가 없고, 호가(FHMIF10010000) 응답의
# output2 에 1~5호가와 호가접수시간 `aspr_acpt_hour` 가 있다. 구조만 본뜬 합성 값.
PRICE_NO_TIME = {
    "rt_cd": "0", "msg_cd": "MCA00000", "msg1": "ok",
    "output1": {"hts_kor_isnm": "미니F 202611", "futs_prpr": "946.70", "futs_prdy_ctrt": "0.1",
                "acml_vol": "10", "basis": "0.30", "futs_last_tr_date": "20261112",
                "hts_rmnn_dynu": "16"},
    "output2": {"bstp_cls_code": "0001", "hts_kor_isnm": "종합", "bstp_nmix_prpr": "6012.44"},
    "output3": {"bstp_cls_code": "2001", "hts_kor_isnm": "KOSPI200", "bstp_nmix_prpr": "946.40"},
}  # fmt: skip
BOOK_OUTPUT2 = {
    "rt_cd": "0", "msg_cd": "MCA00000", "msg1": "ok",
    "output1": {"futs_shrn_iscd": "A05611", "futs_prpr": "946.70"},
    "output2": {"futs_askp1": "946.72", "futs_bidp1": "946.68", "askp_rsqn1": "3",
                "bidp_rsqn1": "4", "aspr_acpt_hour": "101530"},
}  # fmt: skip


def test_fo_price_takes_time_and_top_of_book_from_orderbook(real_client, respx_mock):
    respx_mock.get(REAL + BASE + "/inquire-price").mock(
        return_value=httpx.Response(200, json=PRICE_NO_TIME)
    )
    book = respx_mock.get(REAL + BASE + "/inquire-asking-price").mock(
        return_value=httpx.Response(200, json=BOOK_OUTPUT2)
    )
    q = KisMarket(real_client, now=lambda: T0).get_fo_price("A05611", "FUTURES")
    assert book.calls[0].request.url.params["FID_INPUT_ISCD"] == "A05611"
    assert q.bid == Decimal("946.68") and q.ask == Decimal("946.72")
    assert q.underlying == Decimal("946.40")  # output2 는 코스피 종합 — 업종코드 2001 묶음만
    assert q.quote_asof == datetime(2026, 10, 27, 1, 15, 30, tzinfo=UTC)  # 10:15:30 KST
    assert q.received_at == T0


def test_fo_price_orderbook_failure_leaves_time_none(real_client, respx_mock):
    respx_mock.get(REAL + BASE + "/inquire-price").mock(
        return_value=httpx.Response(200, json=PRICE_NO_TIME)
    )
    respx_mock.get(REAL + BASE + "/inquire-asking-price").mock(
        return_value=httpx.Response(200, json={"rt_cd": "1", "msg_cd": "X", "msg1": "실패"})
    )
    q = KisMarket(real_client, now=lambda: T0).get_fo_price("A05611", "FUTURES")
    assert q.price == Decimal("946.70")  # 시세는 쓸 수 있고
    assert q.quote_asof is None and q.bid is None and q.ask is None  # 진입 근거는 없다


def test_fo_orderbook_reads_output2(real_client, respx_mock):
    respx_mock.get(REAL + BASE + "/inquire-asking-price").mock(
        return_value=httpx.Response(200, json=BOOK_OUTPUT2)
    )
    ob = KisMarket(real_client, now=lambda: T0).get_fo_orderbook("A05611", "FUTURES")
    assert ob.asks[0] == (Decimal("946.72"), 3) and ob.bids[0] == (Decimal("946.68"), 4)
    assert ob.asof == datetime(2026, 10, 27, 1, 15, 30, tzinfo=UTC)
