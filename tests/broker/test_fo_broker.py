"""Phase 26 단계 1: KisFoBroker(엄격 파서·주문·체결·주문 상태)·FakeFoBroker."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
from conftest import make_account
from conftest_broker import load

from trader.broker.errors import UNKNOWN_RESPONSE_CODE, KisApiError
from trader.broker.fake import FakeFoBroker
from trader.broker.fo import CONTRACT_VERSION, KisFoBroker
from trader.broker.models import FoBalance, FoCash, FoOrderRequest, FoOrderState, FoPosition
from trader.config import AccountConfig, FoCfg

PAPER = "https://openapivts.koreainvestment.com:29443"
BASE = "/uapi/domestic-futureoption/v1"
MINI = FoCfg().products["mini"]
T0 = datetime(2026, 10, 27, 1, 0, tzinfo=UTC)


def fo_account(env="paper", mode="auto", id="paper-fo"):
    return AccountConfig(
        id=id, env=env, cano="00000000", acnt_prdt_cd="03", markets=["KR"], mode=mode, asset="fo"
    )


def _broker(client, now=lambda: T0):
    return KisFoBroker(fo_account(), client, product=MINI, now=now)


def test_contract_version_and_account_checks(paper_client):
    assert CONTRACT_VERSION == 2
    with pytest.raises(ValueError, match="asset"):
        KisFoBroker(make_account("paper-main", "paper", "auto"), paper_client, product=MINI)
    with pytest.raises(ValueError, match="env"):
        KisFoBroker(fo_account(env="real", mode="off"), paper_client, product=MINI)


def test_balance_parses_positions_and_cash(paper_client, respx_mock):
    respx_mock.get(PAPER + BASE + "/trading/inquire-balance").mock(
        return_value=httpx.Response(200, json=load("fo_balance.json"))
    )
    ticks = iter([T0, datetime(2026, 10, 27, 1, 0, 1, tzinfo=UTC)])
    b = _broker(paper_client, now=lambda: next(ticks)).get_balance()
    assert isinstance(b, FoBalance)
    assert [p.code for p in b.positions] == ["105W11", "305W11350"]  # 수량 0 행 제외
    f, p = b.positions
    assert f.kind == "FUTURES" and f.contract_side == "LONG" and f.qty == 2
    assert f.avg_price == Decimal("350.00") and f.current_price == Decimal("350.50")
    assert f.sellable_qty == 2 and f.pnl == Decimal("50000")
    assert p.kind == "PUT" and p.contract_side == "SHORT" and p.strike == Decimal("350.0")
    assert b.unverified == []
    c = b.cash
    assert c.nav == Decimal("10050000") and c.orderable_cash == Decimal("8000000")
    assert c.margin_total == Decimal("1500000") and c.add_margin == 0
    assert c.realized_today == Decimal("20000") and c.unrealized == Decimal("50000")
    assert c.requested_at < c.received_at


def test_balance_direction_mismatch_is_unverified(paper_client, respx_mock):
    route = respx_mock.get(PAPER + BASE + "/trading/inquire-balance").mock(
        return_value=httpx.Response(200, json=load("fo_balance_mismatch.json"))
    )
    b = _broker(paper_client).get_balance()
    assert route.calls[0].request.headers["tr_id"] == "VTFO6118R"
    assert [p.code for p in b.positions] == ["305W11352"]
    reasons = {u.code: u.reason for u in b.unverified}
    assert "방향" in reasons["105W11"]  # 이름 매수·코드 01
    assert "종류" in reasons["205W11350"]  # prdt_type_cd 미지
    assert "방향" in reasons["305W11350"]  # 이름 없음
    assert b.cash.nav is None  # prsm_dpast_amt 비면 None


def test_balance_prefix_type_conflict_is_unverified(paper_client, respx_mock):
    data = load("fo_balance.json")
    data["output1"] = [{**data["output1"][0], "prdt_type_cd": "302"}]  # 선물 접두 ↔ 콜 유형
    respx_mock.get(PAPER + BASE + "/trading/inquire-balance").mock(
        return_value=httpx.Response(200, json=data)
    )
    b = _broker(paper_client).get_balance()
    assert b.positions == [] and "종류" in b.unverified[0].reason


def test_balance_page_failure_raises(paper_client, respx_mock):
    first = load("fo_balance.json")
    first["ctx_area_fk200"], first["ctx_area_nk200"] = "k", "n"
    respx_mock.get(PAPER + BASE + "/trading/inquire-balance").mock(
        side_effect=[
            httpx.Response(200, json=first, headers={"tr_cont": "M"}),
            httpx.Response(500, text="oops"),
        ]
    )
    with pytest.raises(KisApiError):
        _broker(paper_client).get_balance()


def test_balance_empty_output2_raises(paper_client, respx_mock):
    data = load("fo_balance.json")
    data["output2"] = {}
    respx_mock.get(PAPER + BASE + "/trading/inquire-balance").mock(
        return_value=httpx.Response(200, json=data)
    )
    with pytest.raises(KisApiError) as e:
        _broker(paper_client).get_balance()
    assert e.value.msg_cd == "INVALID_JSON"


def test_orderable_fields(paper_client, respx_mock):
    route = respx_mock.get(PAPER + BASE + "/trading/inquire-psbl-order").mock(
        return_value=httpx.Response(200, json=load("fo_psbl.json"))
    )
    o = _broker(paper_client).orderable("105W11", "BUY", Decimal("350.50"), "LIMIT")
    assert o.tot_qty == 10 and o.lqd_qty == 0 and o.ord_qty == 10
    assert o.bass_idx == Decimal("350.20") and o.cash is None
    q = route.calls[0].request.url.params
    assert q["PDNO"] == "105W11" and q["SLL_BUY_DVSN_CD"] == "02"
    assert q["UNIT_PRICE"] == "350.5" and q["ORD_DVSN_CD"] == "01"
    _broker(paper_client).orderable("105W11", "SELL", None, "MARKET")
    q = route.calls[1].request.url.params
    assert q["SLL_BUY_DVSN_CD"] == "01" and q["UNIT_PRICE"] == "0" and q["ORD_DVSN_CD"] == "02"


def test_order_body_limit_and_market(paper_client, respx_mock):
    route = respx_mock.post(PAPER + BASE + "/trading/order").mock(
        return_value=httpx.Response(200, json=load("fo_order_ok.json"))
    )
    b = _broker(paper_client)
    res = b.place_order(
        FoOrderRequest(
            code="105W11", side="BUY", qty=2, order_type="LIMIT", price=Decimal("350.50")
        )
    )
    assert res.accepted and res.order_no == "0000012345"
    req = route.calls[0].request
    assert req.headers["tr_id"] == "VTTO1101U"
    body = json.loads(req.content)
    assert body["ORD_PRCS_DVSN_CD"] == "02" and body["SLL_BUY_DVSN_CD"] == "02"
    assert body["SHTN_PDNO"] == "105W11" and body["ORD_QTY"] == "2"
    assert body["UNIT_PRICE"] == "350.5" and body["NMPR_TYPE_CD"] == "01"
    assert body["KRX_NMPR_CNDT_CD"] == "0" and body["ORD_DVSN_CD"] == "01"
    assert body["CANO"] == "00000000" and body["ACNT_PRDT_CD"] == "03"
    b.place_order(FoOrderRequest(code="305W11350", side="SELL", qty=1, order_type="MARKET"))
    body = json.loads(route.calls[1].request.content)
    assert body["SLL_BUY_DVSN_CD"] == "01" and body["UNIT_PRICE"] == "0"
    assert body["NMPR_TYPE_CD"] == "02" and body["ORD_DVSN_CD"] == "02"


def test_order_off_tick_rejected_locally(paper_client, respx_mock):
    route = respx_mock.post(PAPER + BASE + "/trading/order").mock(
        return_value=httpx.Response(200, json=load("fo_order_ok.json"))
    )
    b = _broker(paper_client)
    with pytest.raises(ValueError, match="호가단위"):
        b.place_order(
            FoOrderRequest(
                code="105W11", side="BUY", qty=1, order_type="LIMIT", price=Decimal("350.03")
            )
        )
    with pytest.raises(ValueError, match="호가단위"):
        b.place_order(
            FoOrderRequest(
                code="205W11352", side="BUY", qty=1, order_type="LIMIT", price=Decimal("3.01")
            )
        )
    with pytest.raises(ValueError):  # 지정가인데 가격 없음
        b.place_order(FoOrderRequest(code="105W11", side="BUY", qty=1, order_type="LIMIT"))
    with pytest.raises(ValueError):  # 다른 상품 코드
        b.place_order(FoOrderRequest(code="101W12", side="BUY", qty=1, order_type="MARKET"))
    assert not route.called


def test_order_night_paper_unsupported(paper_client):
    with pytest.raises(KisApiError) as e:
        _broker(paper_client).place_order(
            FoOrderRequest(code="105W11", side="BUY", qty=1, order_type="MARKET"), night=True
        )
    assert e.value.msg_cd == "UNSUPPORTED"


def test_order_timeout_is_network_unknown(paper_client, respx_mock):
    respx_mock.post(PAPER + BASE + "/trading/order").mock(side_effect=httpx.ReadTimeout("t"))
    with pytest.raises(KisApiError) as e:
        _broker(paper_client).place_order(
            FoOrderRequest(code="105W11", side="BUY", qty=1, order_type="MARKET")
        )
    assert e.value.msg_cd == UNKNOWN_RESPONSE_CODE


def test_cancel_body(paper_client, respx_mock):
    route = respx_mock.post(PAPER + BASE + "/trading/order-rvsecncl").mock(
        return_value=httpx.Response(200, json=load("fo_order_ok.json"))
    )
    res = _broker(paper_client).cancel_order("0000000102")
    assert res.accepted
    req = route.calls[0].request
    assert req.headers["tr_id"] == "VTTO1103U"
    body = json.loads(req.content)
    assert body["RVSE_CNCL_DVSN_CD"] == "02" and body["ORGN_ODNO"] == "0000000102"
    assert body["ORD_QTY"] == "0" and body["RMN_QTY_YN"] == "Y" and body["UNIT_PRICE"] == "0"
    _broker(paper_client).cancel_order("0000000102", qty=1)
    body = json.loads(route.calls[1].request.content)
    assert body["ORD_QTY"] == "1" and body["RMN_QTY_YN"] == "N"


def test_fills_parse_and_params(paper_client, respx_mock):
    route = respx_mock.get(PAPER + BASE + "/trading/inquire-ccnl").mock(
        return_value=httpx.Response(200, json=load("fo_ccnl.json"))
    )
    fills = _broker(paper_client).get_fills(date(2026, 10, 27))
    q = route.calls[0].request.url.params
    assert q["STRT_ORD_DT"] == "20261027" and q["END_ORD_DT"] == "20261027"
    assert q["CCLD_NCCS_DVSN"] == "00" and q["SLL_BUY_DVSN_CD"] == "00"
    assert [f.order_no for f in fills] == ["0000000101", "0000000102"]  # 체결 0 행 제외
    f = fills[1]
    assert f.filled_qty == 1 and f.remaining_qty == 2 and f.rejected_qty == 0
    assert f.filled_amount == Decimal("17500000") and f.avg_price == Decimal("350.00")
    assert f.side == "BUY" and f.code == "105W11" and f.orgn_order_no is None
    assert f.filled_at == datetime(2026, 10, 27, 0, 1, tzinfo=UTC)


def test_fills_continuation_failure_raises(paper_client, respx_mock):
    first = load("fo_ccnl.json")
    first["ctx_area_fk200"], first["ctx_area_nk200"] = "k", "n"
    respx_mock.get(PAPER + BASE + "/trading/inquire-ccnl").mock(
        side_effect=[
            httpx.Response(200, json=first, headers={"tr_cont": "F"}),
            httpx.Response(200, json={"rt_cd": "1", "msg_cd": "KIOK9999", "msg1": "실패"}),
        ]
    )
    with pytest.raises(KisApiError):
        _broker(paper_client).get_fills(date(2026, 10, 27))


def test_order_state_terminal_identity(paper_client, respx_mock):
    respx_mock.get(PAPER + BASE + "/trading/inquire-ccnl").mock(
        return_value=httpx.Response(200, json=load("fo_ccnl.json"))
    )
    b = _broker(paper_client)
    d = date(2026, 10, 27)
    s = b.get_order_state("0000000101", d)
    assert s == FoOrderState(
        order_no="0000000101", order_qty=2, filled_qty=2, remaining_qty=0, rejected_qty=0,
        cancelled_qty=0, terminal=True, state="filled",
    )
    s = b.get_order_state("0000000102", d)
    assert s.state == "open" and not s.terminal and s.remaining_qty == 2
    s = b.get_order_state("0000000103", d)
    assert s.state == "rejected" and s.terminal and s.rejected_qty == 1
    s = b.get_order_state("0000000999", d)  # 원주문 행 없음 → open·미확정
    assert s.state == "open" and not s.terminal and s.order_qty == 0


def test_order_state_cancel_lineage(paper_client, respx_mock):
    respx_mock.get(PAPER + BASE + "/trading/inquire-ccnl").mock(
        return_value=httpx.Response(200, json=load("fo_ccnl_cancel.json"))
    )
    b = _broker(paper_client)
    d = date(2026, 10, 27)
    s = b.get_order_state("0000000102", d)
    assert s.state == "cancelled" and s.terminal
    assert s.filled_qty == 1 and s.cancelled_qty == 2 and s.remaining_qty == 0
    # 취소 접수는 됐지만 잔량이 남아 있음 → 미확정
    s = b.get_order_state("0000000106", d)
    assert not s.terminal and s.state == "open" and s.cancelled_qty == 0
    with pytest.raises(KisApiError, match="모순"):
        b.get_order_state("0000000108", d)


def _fake(**kw):
    cash = FoCash(
        deposit=Decimal("10000000"), orderable_cash=Decimal("8000000"),
        orderable_total=Decimal("8000000"), margin_total=Decimal("0"), add_margin=Decimal("0"),
        nav=Decimal("10000000"), realized_today=Decimal("0"), unrealized=Decimal("0"),
        requested_at=T0, received_at=T0,
    )
    bal = FoBalance(positions=[], unverified=[], cash=cash)
    return FakeFoBroker(fo_account(), balance=bal, **kw)


def test_fake_broker_scenarios():
    b = _fake()
    r = b.place_order(FoOrderRequest(code="105W11", side="BUY", qty=1, order_type="MARKET"))
    assert r.order_no == "FAKE-1" and b.orders[0].code == "105W11"
    b.cancel_order("FAKE-1")
    assert b.cancelled == [("FAKE-1", 0)]
    assert b.get_balance().positions == []
    assert b.get_fills(date(2026, 10, 27)) == []
    s = b.get_order_state("FAKE-1", date(2026, 10, 27))
    assert not s.terminal and s.state == "open"
    assert b.orderable("105W11", "BUY", None, "MARKET").ord_qty >= 1
    b2 = _fake(fail={"get_balance"})
    with pytest.raises(KisApiError):
        b2.get_balance()
    b3 = _fake(fail={"place_order:unknown"})
    with pytest.raises(KisApiError) as e:
        b3.place_order(FoOrderRequest(code="105W11", side="BUY", qty=1, order_type="MARKET"))
    assert e.value.msg_cd == UNKNOWN_RESPONSE_CODE
    assert len(b3.orders) == 1  # 요청은 나갔다
    pos = FoPosition(
        code="105W11", name=None, kind="FUTURES", contract_side="LONG", qty=1,
        avg_price=Decimal("350"), current_price=Decimal("350"), pnl=Decimal("0"), sellable_qty=1,
    )
    b4 = _fake()
    b4.balance = b4.balance.model_copy(update={"positions": [pos]})
    assert b4.get_balance().positions[0].code == "105W11"
    assert b4.calls.count("get_balance") == 1


def test_balance_qty_unparseable_is_unverified(paper_client, respx_mock):
    """Codex 단계 1 P0-1: 수량이 결측·비정수·음수인 보유 행은 조용히 빠지지 않고 미확정으로 남는다.
    명시적인 정수 0 만 제외."""
    d = load("fo_balance.json")
    base = dict(d["output1"][0])
    rows = []
    for q in ("?", "1.5", "-1", "", "\u00b2"):  # '²' 는 isdigit True·int() 실패(Claude 리뷰 2)
        r = dict(base)
        r["cblc_qty"] = q
        rows.append(r)
    zero = dict(base)
    zero["cblc_qty"] = "0"
    rows.append(zero)
    d["output1"] = rows
    respx_mock.get(PAPER + BASE + "/trading/inquire-balance").mock(
        return_value=httpx.Response(200, json=d)
    )
    b = _broker(paper_client).get_balance()
    assert b.positions == []
    assert len(b.unverified) == 5 and all("수량" in u.reason for u in b.unverified)


def test_order_state_missing_or_inconsistent_quantities_raise(paper_client, respx_mock):
    """Codex 단계 1 P0-2: 원주문 행의 필수 수량이 비면 예외(완료로 보지 않음), 취소 행 없이
    체결+거부+잔량 ≠ 주문수량이면 모순 예외."""
    d = load("fo_ccnl.json")
    rows = d["output1"]
    only_odno = {"odno": "0000000201", "orgn_odno": "", "ord_dt": "20261027"}
    sup = dict(rows[1])
    sup.update(odno="0000000204", ord_qty="\u00b2")  # 비십진 숫자 문자는 누락과 같다
    short = dict(rows[1])
    short.update(odno="0000000202", ord_qty="3", tot_ccld_qty="1", qty="0", rjct_qty="0")
    over = dict(rows[1])
    over.update(odno="0000000203", ord_qty="3", tot_ccld_qty="3", qty="1", rjct_qty="0")
    d["output1"] = rows + [only_odno, short, over, sup]
    respx_mock.get(PAPER + BASE + "/trading/inquire-ccnl").mock(
        return_value=httpx.Response(200, json=d)
    )
    b = _broker(paper_client)
    dd = date(2026, 10, 27)
    for no in ("0000000201", "0000000204"):
        with pytest.raises(KisApiError, match="누락"):
            b.get_order_state(no, dd)
    for no in ("0000000202", "0000000203"):
        with pytest.raises(KisApiError, match="모순"):
            b.get_order_state(no, dd)
    # 기존 정상 행은 그대로
    assert b.get_order_state("0000000101", dd).terminal
