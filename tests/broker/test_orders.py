import json
from decimal import Decimal

import httpx
import pytest
from conftest import make_account

from trader.broker import tr_ids
from trader.broker.accounts import KisBroker
from trader.broker.errors import KisApiError
from trader.broker.fake import FakeBroker
from trader.broker.models import OrderRequest

PAPER = "https://openapivts.koreainvestment.com:29443"
OK = {
    "rt_cd": "0",
    "msg_cd": "APBK0013",
    "msg1": "주문 전송 완료 되었습니다.",
    "output": {"KRX_FWDG_ORD_ORGNO": "91252", "ODNO": "0000117057", "ORD_TMD": "121052"},
}


def _kr(side="BUY", order_type="LIMIT", price=Decimal("71000"), qty=1):
    return OrderRequest(
        ticker="005930",
        market="KR",
        side=side,
        qty=qty,
        order_type=order_type,
        price=price,
        exchange=None,
    )


def test_kr_limit_order_params_and_tr_id(paper_client, respx_mock):
    route = respx_mock.post(PAPER + "/uapi/domestic-stock/v1/trading/order-cash").mock(
        return_value=httpx.Response(200, json=OK)
    )
    broker = KisBroker(make_account("paper-main", "paper", "auto"), paper_client)
    res = broker.place_order(_kr())
    assert res.accepted and res.order_no == "0000117057" and res.org_no == "91252"
    req = route.calls[0].request
    assert req.headers["tr_id"] == "VTTC0012U"
    body = json.loads(req.content)
    assert body["PDNO"] == "005930" and body["ORD_DVSN"] == "00"
    assert body["ORD_QTY"] == "1" and body["ORD_UNPR"] == "71000"
    assert body["CANO"] == "00000000" and body["ACNT_PRDT_CD"] == "01"
    # 시장가 매도: ORD_DVSN 01, 단가 0, 매도 tr_id
    broker.place_order(_kr(side="SELL", order_type="MARKET", price=None))
    req = route.calls[1].request
    assert req.headers["tr_id"] == "VTTC0011U"
    body = json.loads(req.content)
    assert body["ORD_DVSN"] == "01" and body["ORD_UNPR"] == "0"


def test_us_order_forces_limit_and_exchange_code(paper_client, respx_mock):
    route = respx_mock.post(PAPER + "/uapi/overseas-stock/v1/trading/order").mock(
        return_value=httpx.Response(200, json=OK)
    )
    broker = KisBroker(make_account("paper-main", "paper", "auto", ["KR", "US"]), paper_client)
    req = OrderRequest(
        ticker="AAPL",
        market="US",
        side="BUY",
        qty=1,
        order_type="LIMIT",
        price=Decimal("200.5"),
        exchange="NAS",
    )
    res = broker.place_order(req)
    assert res.accepted and res.order_no == "0000117057"
    body = json.loads(route.calls[0].request.content)
    assert body["OVRS_EXCG_CD"] == "NASD" and body["PDNO"] == "AAPL"
    assert body["OVRS_ORD_UNPR"] == "200.5" and body["ORD_DVSN"] == "00"
    assert route.calls[0].request.headers["tr_id"] == "VTTT1002U"
    # US 시장가는 거부 (모의 서버 제약, 지정가만)
    with pytest.raises(KisApiError) as e:
        broker.place_order(req.model_copy(update={"order_type": "MARKET", "price": None}))
    assert e.value.msg_cd == "UNSUPPORTED"
    # exchange 없는 US 주문은 ValueError
    with pytest.raises(ValueError, match="exchange"):
        broker.place_order(req.model_copy(update={"exchange": None}))


def test_order_rejected_is_error(paper_client, respx_mock):
    respx_mock.post(PAPER + "/uapi/domestic-stock/v1/trading/order-cash").mock(
        return_value=httpx.Response(
            200, json={"rt_cd": "1", "msg_cd": "APBK0919", "msg1": "주문가능금액을 초과했습니다"}
        )
    )
    broker = KisBroker(make_account("paper-main", "paper", "auto"), paper_client)
    with pytest.raises(KisApiError) as e:
        broker.place_order(_kr())
    assert e.value.msg_cd == "APBK0919" and e.value.tr_id == "VTTC0012U"


def test_market_not_in_account_rejected_for_order(paper_client):
    broker = KisBroker(make_account("paper-main", "paper", "auto", ["KR"]), paper_client)
    with pytest.raises(ValueError, match="US"):
        broker.place_order(
            OrderRequest(
                ticker="AAPL",
                market="US",
                side="BUY",
                qty=1,
                order_type="LIMIT",
                price=Decimal("1"),
                exchange="NAS",
            )
        )


def test_cancel_kr_order(paper_client, respx_mock):
    route = respx_mock.post(PAPER + "/uapi/domestic-stock/v1/trading/order-rvsecncl").mock(
        return_value=httpx.Response(200, json=OK)
    )
    broker = KisBroker(make_account("paper-main", "paper", "auto"), paper_client)
    res = broker.cancel_order("0000117057", market="KR", org_no="91252")
    assert res.accepted
    req = route.calls[0].request
    assert req.headers["tr_id"] == "VTTC0013U"
    body = json.loads(req.content)
    assert body["ORGN_ODNO"] == "0000117057" and body["RVSE_CNCL_DVSN_CD"] == "02"
    assert body["QTY_ALL_ORD_YN"] == "Y" and body["KRX_FWDG_ORD_ORGNO"] == "91252"


def test_cancel_tr_ids_registered():
    assert tr_ids.tr_id("DOM_ORDER_CANCEL", "paper") == "VTTC0013U"
    assert tr_ids.tr_id("OVS_ORDER_CANCEL_US", "real") == "TTTT1004U"


def test_fake_broker_records():
    b = FakeBroker(make_account("paper-main", "paper", "auto"))
    r1 = b.place_order(_kr())
    r2 = b.place_order(_kr(side="SELL"))
    assert (r1.order_no, r2.order_no) == ("FAKE-1", "FAKE-2") and r1.accepted
    assert [o.side for o in b.orders] == ["BUY", "SELL"]
    assert b.cancel_order("FAKE-1", market="KR").accepted and b.cancelled == ["FAKE-1"]
    b2 = FakeBroker(make_account("paper-main", "paper", "auto"), fail={"place_order"})
    with pytest.raises(KisApiError):
        b2.place_order(_kr())


def test_price_str_rejects_off_tick_price():
    """호가단위를 벗어난 가격은 KIS 에 보내기 전에 ValueError 로 막는다 (KR 소수, US 소수 5자리)."""
    from trader.broker.accounts import _price_str

    assert _price_str(Decimal("71000")) == "71000" and _price_str(Decimal("200.5")) == "200.5"
    with pytest.raises(ValueError):
        _price_str(Decimal("70350.5"), market="KR")
    with pytest.raises(ValueError):
        _price_str(Decimal("253.12345"), market="US")
    assert _price_str(Decimal("0.1234"), market="US") == "0.1234"  # 1달러 미만은 0.0001
