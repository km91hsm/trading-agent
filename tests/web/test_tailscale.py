"""폰 접속(Stock D-054): tailscale serve 가 넘겨 주는 tailnet 이름 요청은 허용 로그인 + 루프백만,
설정이 비면 지금처럼 로컬만."""

from fastapi.testclient import TestClient
from helpers_web import make_deps

from trader.config import Settings
from trader.web.app import create_app

TS = "mymac.tail1234.ts.net"


def _client(project, *, hosts=(TS,), users=("me@example.com",), peer="127.0.0.1"):
    deps = make_deps(project)
    deps.settings = Settings(web={"tailscale_hosts": list(hosts), "tailscale_users": list(users)})
    return TestClient(create_app(deps), client=(peer, 50000))


def test_allowed_user_via_tailnet(project, conn):
    c = _client(project)
    ok = c.get("/api/system/health", headers={"host": TS, "tailscale-user-login": "me@example.com"})
    assert ok.status_code == 200
    bad = c.get("/api/system/health", headers={"host": TS, "tailscale-user-login": "x@y.z"})
    assert bad.status_code == 403
    assert c.get("/api/system/health", headers={"host": TS}).status_code == 403


def test_tailnet_host_from_non_loopback_rejected(project, conn):
    c = _client(project, peer="100.64.0.9")
    r = c.get("/api/system/health", headers={"host": TS, "tailscale-user-login": "me@example.com"})
    assert r.status_code == 403


def test_unconfigured_host_still_rejected(project, conn):
    c = _client(project, hosts=(), users=())
    assert c.get("/api/system/health", headers={"host": TS}).status_code == 400
    assert c.get("/api/system/health").status_code == 200  # 로컬 그대로


def test_proxied_request_cannot_bypass_with_localhost_host(project, conn):
    """프록시를 거친 요청(X-Forwarded-For·Tailscale-User-Login)은 Host 와 무관하게 사용자 검사."""
    c = _client(project)
    r = c.get("/api/system/health", headers={"x-forwarded-for": "100.64.0.9"})
    assert r.status_code == 403
    r = c.get("/api/system/health", headers={"tailscale-user-login": "x@y.z"})
    assert r.status_code == 403
