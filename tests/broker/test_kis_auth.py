import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from trader.broker.errors import KisApiError
from trader.broker.kis_auth import KisAuth

TOKEN_URL = "https://openapivts.koreainvestment.com:29443/oauth2/tokenP"
T0 = datetime(2026, 10, 9, 0, 0, tzinfo=UTC)


def make_auth(project, clock, http=None):
    return KisAuth("paper", "APPKEY-SECRET-1", "APPSECRET-X", http=http, clock=clock)


def token_response(token="tok1", seconds=86400):
    return httpx.Response(200, json={"access_token": token, "expires_in": seconds})


@respx.mock
def test_issues_and_caches(project):
    route = respx.post(TOKEN_URL).mock(return_value=token_response())
    auth = make_auth(project, lambda: T0)
    assert auth.token() == "tok1"
    cache = json.loads((project / "state" / "kis_token_paper.json").read_text(encoding="utf-8"))
    assert cache["access_token"] == "tok1" and cache["expires_at"].endswith("+00:00")
    body = json.loads(route.calls[0].request.content)
    assert body["appkey"] == "APPKEY-SECRET-1" and body["grant_type"] == "client_credentials"
    assert auth.headers()["authorization"] == "Bearer tok1"


@respx.mock
def test_reuses_valid_cache(project):
    route = respx.post(TOKEN_URL).mock(return_value=token_response())
    make_auth(project, lambda: T0).token()
    make_auth(project, lambda: T0 + timedelta(hours=1)).token()
    assert route.call_count == 1


@respx.mock
def test_refreshes_near_expiry(project):
    route = respx.post(TOKEN_URL).mock(side_effect=[token_response("tok1"), token_response("tok2")])
    make_auth(project, lambda: T0).token()
    # 24h 만료, 23h30m 시점이면 만료 1시간 전 → 재발급
    assert make_auth(project, lambda: T0 + timedelta(hours=23, minutes=30)).token() == "tok2"
    assert route.call_count == 2


@respx.mock
def test_corrupt_cache_refetches(project):
    respx.post(TOKEN_URL).mock(return_value=token_response("fresh"))
    (project / "state" / "kis_token_paper.json").write_text("{not json", encoding="utf-8")
    assert make_auth(project, lambda: T0).token() == "fresh"
    (project / "state" / "kis_token_paper.json").write_text(
        '{"access_token": "x"}', encoding="utf-8"
    )
    assert make_auth(project, lambda: T0).token() == "fresh"


@respx.mock
def test_backoff_after_failed_issue(project):
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(403, json={"msg": "no"}))
    now = [T0]
    auth = make_auth(project, lambda: now[0])
    with pytest.raises(KisApiError) as e1:
        auth.token()
    assert e1.value.msg_cd == "HTTP403"
    now[0] = T0 + timedelta(seconds=30)
    with pytest.raises(KisApiError) as e2:
        auth.token()
    assert e2.value.msg_cd == "AUTH" and route.call_count == 1
    now[0] = T0 + timedelta(seconds=61)
    with pytest.raises(KisApiError):
        auth.token()
    assert route.call_count == 2


@respx.mock
def test_refresh_failure_uses_still_valid_token(project):
    """갱신 창(만료 1시간 전)에서 발급이 실패해도 아직 유효한 캐시 토큰을 쓴다 (리뷰 I2)."""
    respx.post(TOKEN_URL).mock(
        side_effect=[token_response("tok1"), httpx.Response(403, json={"msg": "1분 1회"})]
    )
    make_auth(project, lambda: T0).token()
    auth = make_auth(project, lambda: T0 + timedelta(hours=23, minutes=30))
    assert auth.token() == "tok1"


@respx.mock
def test_failed_at_persisted_across_instances(project):
    """발급 실패 백오프는 인스턴스가 아니라 캐시 파일에 기록돼 다른 인스턴스에도 적용된다."""
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(403, json={"msg": "no"}))
    with pytest.raises(KisApiError):
        make_auth(project, lambda: T0).token()
    with pytest.raises(KisApiError) as e:
        make_auth(project, lambda: T0 + timedelta(seconds=10)).token()
    assert e.value.msg_cd == "AUTH" and route.call_count == 1


@respx.mock
def test_invalidate_removes_cache(project):
    respx.post(TOKEN_URL).mock(return_value=token_response("tok1"))
    auth = make_auth(project, lambda: T0)
    auth.token()
    auth.invalidate()
    assert not auth.cache_path.exists()


@respx.mock
def test_secret_not_in_error_message(project):
    respx.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(KisApiError) as e:
        make_auth(project, lambda: T0).token()
    assert e.value.msg_cd == "NETWORK"
    assert "APPKEY-SECRET-1" not in str(e.value) and "APPSECRET-X" not in str(e.value)


@respx.mock
def test_failed_issue_does_not_overwrite_token_written_by_other_process(project):
    """데몬(A)과 tool 서브프로세스(B)가 같은 갱신 창에서 발급을 시도하는 경우.

    A 가 NEW 를 쓴 뒤 B 가 거부되면 B 는 읽어 둔 OLD 스냅샷을 다시 써서 NEW 를 지웠다.
    실패 기록은 다시 읽은 캐시에 덧붙여야 한다."""
    a = make_auth(project, lambda: T0)
    respx.post(TOKEN_URL).mock(return_value=token_response("NEW"))
    assert a.token() == "NEW"
    b = make_auth(project, lambda: T0)
    stale = {"token": "OLD", "expires_at": T0 + timedelta(minutes=30)}
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(403, json={"msg": "1분 1회"}))
    with pytest.raises(KisApiError):
        b._issue(stale)
    cache = json.loads(a.cache_path.read_text(encoding="utf-8"))
    assert cache["access_token"] == "NEW" and cache.get("failed_at")
    assert a.token() == "NEW"
