import httpx
import pytest
import respx

from trader.broker.errors import KisApiError
from trader.broker.kis_client import RateLimiter

REAL = "https://openapi.koreainvestment.com:9443"
PAPER = "https://openapivts.koreainvestment.com:29443"
PRICE = "/uapi/domestic-stock/v1/quotations/inquire-price"


def ok(output: dict) -> httpx.Response:
    return httpx.Response(
        200, json={"rt_cd": "0", "msg_cd": "MCA00000", "msg1": "정상", "output": output}
    )


def test_headers_include_tr_id(real_client, respx_mock):
    route = respx_mock.get(REAL + PRICE).mock(return_value=ok({"stck_prpr": "71500"}))
    data = real_client.get(PRICE, "DOM_PRICE", {"FID_INPUT_ISCD": "005930"})
    assert data["output"]["stck_prpr"] == "71500"
    h = route.calls[0].request.headers
    assert h["tr_id"] == "FHKST01010100" and h["authorization"] == "Bearer tok"
    assert h["appkey"] == "key" and h["custtype"] == "P"
    assert route.calls[0].request.url.params["FID_INPUT_ISCD"] == "005930"


def test_paper_uses_paper_domain_and_tr_id(paper_client, respx_mock):
    route = respx_mock.get(PAPER + "/uapi/domestic-stock/v1/trading/inquire-balance").mock(
        return_value=ok({})
    )
    paper_client.get("/uapi/domestic-stock/v1/trading/inquire-balance", "DOM_BALANCE", {})
    assert route.calls[0].request.headers["tr_id"] == "VTTC8434R"


def test_business_error_and_http_error(real_client, respx_mock):
    respx_mock.get(REAL + PRICE).mock(
        side_effect=[
            httpx.Response(200, json={"rt_cd": "1", "msg_cd": "OPSQ0001", "msg1": "없는 종목"}),
            httpx.Response(500, text="oops"),
        ]
    )
    with pytest.raises(KisApiError) as e1:
        real_client.get(PRICE, "DOM_PRICE", {})
    assert (e1.value.msg_cd, e1.value.tr_id) == ("OPSQ0001", "FHKST01010100")
    with pytest.raises(KisApiError) as e2:
        real_client.get(PRICE, "DOM_PRICE", {})
    assert e2.value.msg_cd == "HTTP500" and "secret" not in str(e2.value)


def test_invalid_json(real_client, respx_mock):
    respx_mock.get(REAL + PRICE).mock(return_value=httpx.Response(200, text="<html>"))
    with pytest.raises(KisApiError) as e:
        real_client.get(PRICE, "DOM_PRICE", {})
    assert e.value.msg_cd == "INVALID_JSON"


def test_network_error(real_client, respx_mock):
    respx_mock.get(REAL + PRICE).mock(side_effect=httpx.ConnectError("x"))
    with pytest.raises(KisApiError) as e:
        real_client.get(PRICE, "DOM_PRICE", {})
    assert e.value.msg_cd == "NETWORK"


def test_egw00201_retry_once(real_client, respx_mock, monkeypatch):
    slept = []
    monkeypatch.setattr(real_client, "_sleep", lambda s: slept.append(s))
    route = respx_mock.get(REAL + PRICE).mock(
        side_effect=[
            httpx.Response(
                200, json={"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수 초과"}
            ),
            ok({"stck_prpr": "1"}),
        ]
    )
    assert real_client.get(PRICE, "DOM_PRICE", {})["output"]["stck_prpr"] == "1"
    assert route.call_count == 2 and slept == [0.5]


def test_egw00201_as_http500_retries_once(paper_client, respx_mock, monkeypatch):
    """실기 확인: KIS 는 초당 한도 초과를 HTTP 500 + JSON 본문으로도 돌려준다."""
    slept = []
    monkeypatch.setattr(paper_client, "_sleep", lambda s: slept.append(s))
    url = PAPER + "/uapi/domestic-stock/v1/trading/inquire-balance"
    route = respx_mock.get(url).mock(
        side_effect=[
            httpx.Response(
                500,
                json={
                    "rt_cd": "1",
                    "msg1": "초당 거래건수를 초과하였습니다.",
                    "msg_cd": "EGW00201",
                },
            ),
            ok({"x": "1"}),
        ]
    )
    data = paper_client.get("/uapi/domestic-stock/v1/trading/inquire-balance", "DOM_BALANCE", {})
    assert data["output"]["x"] == "1" and route.call_count == 2 and slept == [0.5]


def test_http500_with_kis_code_keeps_code(real_client, respx_mock, monkeypatch):
    """HTTP≠200 이라도 KIS 본문의 msg_cd 를 유지한다. EGW00201 이 재시도 상한(3회)을 넘겨 계속되면
    그 코드로 끝난다."""
    monkeypatch.setattr(real_client, "_sleep", lambda s: None)
    limited = httpx.Response(500, json={"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초과"})
    respx_mock.get(REAL + PRICE).mock(side_effect=[limited, limited, limited, limited])
    with pytest.raises(KisApiError) as e:
        real_client.get(PRICE, "DOM_PRICE", {})
    assert e.value.msg_cd == "EGW00201"


def test_token_error_invalidates_and_retries(real_client, respx_mock, monkeypatch):
    """만료 토큰(EGW00123)이면 캐시를 지우고 재발급 후 1회 재시도한다 (리뷰 Important 3)."""
    invalidated = []
    monkeypatch.setattr(real_client._auth, "invalidate", lambda: invalidated.append(1))
    route = respx_mock.get(REAL + PRICE).mock(
        side_effect=[
            httpx.Response(500, json={"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "만료"}),
            ok({"stck_prpr": "1"}),
        ]
    )
    assert real_client.get(PRICE, "DOM_PRICE", {})["output"]["stck_prpr"] == "1"
    assert invalidated == [1] and route.call_count == 2


def test_missing_rt_cd_is_invalid(real_client, respx_mock):
    respx_mock.get(REAL + PRICE).mock(
        return_value=httpx.Response(200, json={"error_code": "EGW00103", "error_description": "x"})
    )
    with pytest.raises(KisApiError) as e:
        real_client.get(PRICE, "DOM_PRICE", {})
    assert e.value.msg_cd == "EGW00103"


def test_post_sends_json_body(paper_client, respx_mock):
    route = respx_mock.post(PAPER + "/uapi/domestic-stock/v1/trading/order-cash").mock(
        return_value=ok({"ODNO": "1"})
    )
    paper_client.post(
        "/uapi/domestic-stock/v1/trading/order-cash", "DOM_ORDER_BUY", {"PDNO": "005930"}
    )
    assert route.calls[0].request.headers["tr_id"] == "VTTC0012U"
    assert b'"PDNO"' in route.calls[0].request.content


def test_rate_limit_spacing():
    now = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        now[0] += s

    rl = RateLimiter(20, clock=lambda: now[0], sleep=sleep)
    for _ in range(21):
        rl.acquire()
    assert slept and min(slept) > 0  # 21번째 호출은 1초 창을 넘길 때까지 대기
    assert now[0] >= 1.0 - 1e-6


@respx.mock
def test_paper_client_rejects_unsupported_tr(paper_client):
    with pytest.raises(KisApiError) as e:
        paper_client.get("/x", "HOLIDAY", {})
    assert e.value.msg_cd == "UNSUPPORTED"


def test_timeout_after_send_is_unknown_response_not_network(real_client, respx_mock):
    """요청이 나간 뒤 응답을 못 받은 경우(ReadTimeout)는 주문이 살아 있을 수 있어
    '보내지도 못한' ConnectError(NETWORK)와 구분한다."""
    respx_mock.post(REAL + "/uapi/domestic-stock/v1/trading/order-cash").mock(
        side_effect=[httpx.ReadTimeout("slow"), httpx.ConnectError("down")]
    )
    with pytest.raises(KisApiError) as e1:
        real_client.post("/uapi/domestic-stock/v1/trading/order-cash", "DOM_ORDER_BUY", {})
    assert e1.value.msg_cd == "NETWORK_UNKNOWN"
    with pytest.raises(KisApiError) as e2:
        real_client.post("/uapi/domestic-stock/v1/trading/order-cash", "DOM_ORDER_BUY", {})
    assert e2.value.msg_cd == "NETWORK"


def test_egw00201_backs_off_up_to_three_times(real_client, respx_mock, monkeypatch):
    """백필(실전 20/s)에서 한 번 재시도로는 모자라 종목이 통째로 빠졌다(실기 10-20: 034730·AMZN).
    0.5 → 1.0 → 2.0 초로 최대 3회 물러난 뒤에도 초과면 그때 올린다."""
    slept = []
    monkeypatch.setattr(real_client, "_sleep", lambda s: slept.append(s))
    limited = httpx.Response(200, json={"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초과"})
    route = respx_mock.get(REAL + PRICE).mock(
        side_effect=[limited, limited, limited, ok({"stck_prpr": "1"})]
    )
    assert real_client.get(PRICE, "DOM_PRICE", {})["output"]["stck_prpr"] == "1"
    assert route.call_count == 4 and slept == [0.5, 1.0, 2.0]
    respx_mock.get(REAL + PRICE).mock(side_effect=[limited, limited, limited, limited])
    with pytest.raises(KisApiError) as e:
        real_client.get(PRICE, "DOM_PRICE", {})
    assert e.value.msg_cd == "EGW00201"
