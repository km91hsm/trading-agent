from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
from conftest import make_account
from conftest_broker import load

from trader.broker import tr_ids
from trader.broker.accounts import KisBroker

PAPER = "https://openapivts.koreainvestment.com:29443"


def test_ccld_tr_ids_registered():
    assert tr_ids.tr_id("DOM_CCLD", "paper") == "VTTC0081R"
    assert tr_ids.tr_id("OVS_CCLD_US", "real") == "TTTS3035R"
    assert tr_ids.path("DOM_CCLD").endswith("inquire-daily-ccld")


def test_get_fills_kr_params_and_parse(paper_client, respx_mock):
    route = respx_mock.get(PAPER + "/uapi/domestic-stock/v1/trading/inquire-daily-ccld").mock(
        return_value=httpx.Response(200, json=load("ccld_kr.json"))
    )
    broker = KisBroker(make_account("paper-main", "paper", "auto"), paper_client)
    fills = broker.get_fills("KR", date(2026, 10, 9))
    p = route.calls[0].request.url.params
    assert p["INQR_STRT_DT"] == "20261009" and p["INQR_END_DT"] == "20261009"
    assert p["CCLD_DVSN"] == "01" and p["SLL_BUY_DVSN_CD"] == "00"
    assert route.calls[0].request.headers["tr_id"] == "VTTC0081R"
    assert len(fills) == 1  # 체결수량 0 제외
    f = fills[0]
    assert f.order_no == "0000117057" and f.ticker == "005930" and f.side == "BUY"
    assert f.qty == 3 and f.price == Decimal("70900") and f.market == "KR"
    assert f.filled_at == datetime(2026, 10, 9, 1, 5, 12, tzinfo=UTC)  # 10:05:12 KST


def test_get_fills_us_parse(paper_client, respx_mock):
    route = respx_mock.get(PAPER + "/uapi/overseas-stock/v1/trading/inquire-ccnl").mock(
        return_value=httpx.Response(200, json=load("ccld_us.json"))
    )
    broker = KisBroker(make_account("paper-main", "paper", "auto", ["KR", "US"]), paper_client)
    fills = broker.get_fills("US", date(2026, 10, 8))
    p = route.calls[0].request.url.params
    assert p["ORD_STRT_DT"] == "20261008" and p["OVRS_EXCG_CD"] == "NASD"
    assert len(fills) == 1 and fills[0].ticker == "AAPL" and fills[0].price == Decimal("200.4500")
    assert fills[0].filled_at == datetime(2026, 10, 8, 13, 31, 1, tzinfo=UTC)  # 09:31:01 ET
