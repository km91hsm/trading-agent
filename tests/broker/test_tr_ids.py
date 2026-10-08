import pytest

from trader.broker import tr_ids
from trader.broker.errors import KisApiError


def test_base_urls():
    assert tr_ids.BASE_URL["real"].startswith("https://openapi.koreainvestment.com")
    assert tr_ids.BASE_URL["paper"].startswith("https://openapivts.koreainvestment.com")


def test_tr_id_real_and_paper():
    assert tr_ids.tr_id("DOM_BALANCE", "real") == "TTTC8434R"
    assert tr_ids.tr_id("DOM_BALANCE", "paper") == "VTTC8434R"
    assert tr_ids.tr_id("DOM_PRICE", "paper") == "FHKST01010100"
    assert tr_ids.path("DOM_PRICE") == "/uapi/domestic-stock/v1/quotations/inquire-price"


def test_tr_id_unsupported_on_paper():
    with pytest.raises(KisApiError) as e:
        tr_ids.tr_id("HOLIDAY", "paper")
    assert e.value.msg_cd == "UNSUPPORTED"


def test_unknown_name():
    with pytest.raises(KeyError):
        tr_ids.tr_id("NOPE", "real")


def test_order_ids_registered():
    assert tr_ids.tr_id("DOM_ORDER_BUY", "real") == "TTTC0012U"
    assert tr_ids.tr_id("DOM_ORDER_SELL", "paper") == "VTTC0011U"
    assert tr_ids.tr_id("OVS_ORDER_BUY_US", "paper") == "VTTT1002U"
    assert tr_ids.tr_id("OVS_ORDER_SELL_US", "real") == "TTTT1006U"
    assert tr_ids.tr_id("OVS_BALANCE", "paper") == "VTTS3012R"
