"""KIS 실시간 웹소켓: 프레임 해석(여러 건 묶음·부호), 핑퐁, 제어 메시지, 접속키 발급."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from helpers_kis_ws import frame, trade_values

from trader.broker import kis_ws
from trader.broker.errors import KisApiError
from trader.broker.kis_auth import KisAuth

APPROVAL_URL = "https://openapi.koreainvestment.com:9443/oauth2/Approval"
T0 = datetime(2026, 10, 27, 1, 0, tzinfo=UTC)


def test_parse_frame_splits_batched_records_and_signs():
    raw = frame(trade_values("005930", 71500), trade_values("000660", 180000, "5", "2.10"))
    rows = kis_ws.parse_frame(raw)
    assert [(r["ticker"], r["price"], r["change_pct"]) for r in rows] == [
        ("005930", "71500", 0.0125),  # REST 와 같은 비율 단위
        ("000660", "180000", -0.021),  # 부호 4·5 는 하락(값이 양수로 와도)
    ]


def test_parse_frame_ignores_encrypted_and_garbage():
    assert kis_ws.parse_frame("1|H0STCNI0|001|abcd") == []
    assert kis_ws.parse_frame("not a frame") == []
    assert kis_ws.parse_frame("0|H0STCNT0|001|005930^093015") == []  # 가격 칸 없음


def test_pingpong_and_control():
    ping = json.dumps({"header": {"tr_id": "PINGPONG", "datetime": "20261027093000"}})
    assert kis_ws.is_pingpong(ping) and not kis_ws.is_pingpong("0|H0STCNT0|001|x")
    ok = {"header": {"tr_id": "H0STCNT0", "tr_key": "005930"}, "body": {"rt_cd": "0"}}
    bad = {
        "header": {"tr_id": "H0STCNT0", "tr_key": "999999"},
        "body": {"rt_cd": "1", "msg1": "invalid tr_key"},
    }
    assert kis_ws.control_problem(json.dumps(ok)) is None
    assert kis_ws.control_problem(json.dumps(bad)) == ("H0STCNT0", "999999", "invalid tr_key")
    assert kis_ws.control_problem("0|H0STCNT0|001|x") is None


def test_subscribe_message_shape():
    msg = json.loads(kis_ws.subscribe_message("KEY", "005930"))
    assert msg["header"] == {
        "approval_key": "KEY",
        "custtype": "P",
        "tr_type": "1",
        "content-type": "utf-8",
    }
    assert msg["body"]["input"] == {"tr_id": "H0STCNT0", "tr_key": "005930"}
    assert (
        json.loads(kis_ws.subscribe_message("KEY", "005930", release=True))["header"]["tr_type"]
        == "2"
    )


@respx.mock
def test_approval_key_issue_cache_invalidate(project):
    route = respx.post(APPROVAL_URL).mock(
        return_value=httpx.Response(200, json={"approval_key": "ak-1"})
    )
    clock = [T0]
    auth = KisAuth("real", "APPKEY", "SECRET", clock=lambda: clock[0])
    assert auth.approval_key() == "ak-1" and auth.approval_key() == "ak-1"
    assert route.call_count == 1
    body = json.loads(route.calls[0].request.content)
    assert body == {"grant_type": "client_credentials", "appkey": "APPKEY", "secretkey": "SECRET"}
    auth.invalidate_approval()
    auth.approval_key()
    assert route.call_count == 2
    clock[0] = T0 + timedelta(hours=21)  # 수명(20시간) 지나면 새로
    auth.approval_key()
    assert route.call_count == 3


@respx.mock
def test_approval_key_failure_does_not_leak_secret(project):
    respx.post(APPROVAL_URL).mock(return_value=httpx.Response(403, json={"error": "x"}))
    auth = KisAuth("real", "APPKEY", "SECRET")
    with pytest.raises(KisApiError) as e:
        auth.approval_key()
    assert "SECRET" not in str(e.value) and "APPKEY" not in str(e.value)
