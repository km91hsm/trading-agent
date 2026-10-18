from decimal import Decimal

import httpx
import pytest
from conftest import make_account

from trader.broker.accounts import KisBroker

PAPER = "https://openapivts.koreainvestment.com:29443"


def _route(respx_mock, path, fixture, load):
    return respx_mock.get(PAPER + path).mock(return_value=httpx.Response(200, json=load(fixture)))


def test_env_mismatch_rejected(paper_client):
    with pytest.raises(ValueError, match="env"):
        KisBroker(make_account("real-main", "real", "off"), paper_client)


def test_positions_kr_parsed(paper_client, respx_mock):
    from conftest_broker import load

    route = _route(
        respx_mock, "/uapi/domestic-stock/v1/trading/inquire-balance", "balance_kr.json", load
    )
    broker = KisBroker(make_account("paper-main", "paper", "auto"), paper_client)
    positions = broker.get_positions("KR")
    assert [p.ticker for p in positions] == ["005930"]  # 수량 0 은 제외
    p = positions[0]
    assert (
        p.qty == 10 and p.avg_price == Decimal("70000.0000") and p.eval_amount == Decimal("715000")
    )
    assert p.pnl_pct == pytest.approx(0.0214)
    params = route.calls[0].request.url.params
    assert params["CANO"] == "00000000" and params["ACNT_PRDT_CD"] == "01"
    assert params["INQR_DVSN"] == "02"


def test_cash_kr_parsed(paper_client, respx_mock):
    from conftest_broker import load

    _route(respx_mock, "/uapi/domestic-stock/v1/trading/inquire-balance", "balance_kr.json", load)
    cash = KisBroker(make_account("paper-main", "paper", "auto"), paper_client).get_cash("KR")
    assert cash.currency == "KRW" and cash.total == Decimal("9300000")
    assert cash.available == Decimal("9300000")


def test_positions_and_cash_us_parsed(paper_client, respx_mock):
    from conftest_broker import load

    r1 = _route(
        respx_mock, "/uapi/overseas-stock/v1/trading/inquire-balance", "balance_us.json", load
    )
    r2 = _route(
        respx_mock,
        "/uapi/overseas-stock/v1/trading/inquire-present-balance",
        "present_balance_us.json",
        load,
    )
    broker = KisBroker(make_account("paper-main", "paper", "auto", ["KR", "US"]), paper_client)
    pos = broker.get_positions("US")
    assert (
        pos[0].ticker == "AAPL" and pos[0].qty == 3 and pos[0].current_price == Decimal("110.0000")
    )
    assert r1.calls[0].request.url.params["OVRS_EXCG_CD"] == "NASD"
    cash = broker.get_cash("US")
    assert cash.currency == "USD"
    assert cash.available == Decimal("1200.00") and cash.total == Decimal("1250.50")
    p = r2.calls[0].request.url.params
    assert p["WCRC_FRCR_DVSN_CD"] == "02" and p["NATN_CD"] == "840"
    assert r2.calls[0].request.headers["tr_id"] == "VTRP6504R"


def test_cash_us_without_usd_row_is_zero(paper_client, respx_mock):
    from conftest_broker import load

    data = load("present_balance_us.json")
    data["output2"] = []
    respx_mock.get(PAPER + "/uapi/overseas-stock/v1/trading/inquire-present-balance").mock(
        return_value=httpx.Response(200, json=data)
    )
    broker = KisBroker(make_account("paper-main", "paper", "auto", ["KR", "US"]), paper_client)
    cash = broker.get_cash("US")
    assert cash.available == Decimal("0") and cash.total == Decimal("0")


def test_market_not_in_account_rejected(paper_client):
    broker = KisBroker(make_account("paper-main", "paper", "auto", ["KR"]), paper_client)
    with pytest.raises(ValueError, match="US"):
        broker.get_positions("US")


def test_get_balance_kr_single_request(paper_client, respx_mock):
    """build_portfolio 가 get_positions + get_cash 를 따로 부르면 같은 inquire-balance 를 2번 친다
    (모의 2/s). 한 응답에서 둘을 뽑는다."""
    from conftest_broker import load

    route = _route(
        respx_mock, "/uapi/domestic-stock/v1/trading/inquire-balance", "balance_kr.json", load
    )
    b = KisBroker(make_account("paper-main", "paper", "auto"), paper_client)
    positions, cash = b.get_balance("KR")
    assert route.call_count == 1
    assert [p.ticker for p in positions] == [p.ticker for p in b.get_positions("KR")]
    assert cash == b.get_cash("KR")


def test_fake_broker_get_balance():
    from decimal import Decimal as D

    from trader.broker.fake import FakeBroker
    from trader.broker.models import Cash

    a = make_account("paper-main", "paper", "auto")
    cash = {"KR": Cash(currency="KRW", available=D(1), total=D(1))}
    fb = FakeBroker(a, cash=cash)
    assert fb.get_balance("KR") == ([], cash["KR"])
