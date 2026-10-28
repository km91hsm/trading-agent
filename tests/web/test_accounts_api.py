"""설정 화면의 계좌 추가·삭제 API: 토큰 + 이 PC 직접 접속에서만, 바꾼 뒤 restart_required,
규칙 위반은 400, 살아 있는 주문 등으로 못 지우면 409."""

from fastapi.testclient import TestClient
from helpers_web import AUTH

from trader.web.app import create_app

LOCAL = ("127.0.0.1", 50000)

YAML = """\
accounts:
  - {id: paper-main, env: paper, cano: "00000000", markets: [KR, US], mode: auto}
  - {id: real-main, env: real, cano: "00000000", acnt_prdt_cd: "01", markets: [KR], mode: "off"}
"""


def _client(deps):
    return TestClient(create_app(deps), client=LOCAL, base_url="http://127.0.0.1")


def _yml(project):
    p = project / "config" / "accounts.yaml"
    p.write_text(YAML, encoding="utf-8")
    return p


def test_requires_token_and_direct_local(project, deps):
    _yml(project)
    c = _client(deps)
    body = {"id": "real-2", "env": "real", "markets": ["KR"]}
    assert c.post("/api/control/accounts", json=body).status_code == 401
    assert c.post("/api/control/accounts/remove", json={"account_id": "x"}).status_code == 401
    proxied = c.post(
        "/api/control/accounts",
        headers={**AUTH, "Tailscale-User-Login": "me@example.com", "X-Forwarded-For": "100.1.2.3"},
        json=body,
    )
    assert proxied.status_code == 403
    remote = TestClient(create_app(deps), client=("100.64.0.9", 1), base_url="http://127.0.0.1")
    assert remote.post("/api/control/accounts", headers=AUTH, json=body).status_code == 403


def test_add_and_remove(project, deps):
    p = _yml(project)
    c = _client(deps)
    r = c.post(
        "/api/control/accounts",
        headers=AUTH,
        json={"id": "real-2", "env": "real", "asset": "stock", "markets": ["KR", "US"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["restart_required"] is True and r.json()["id"] == "real-2"
    assert "real-2" in p.read_text(encoding="utf-8")

    r = c.post("/api/control/accounts/remove", headers=AUTH, json={"account_id": "real-2"})
    assert r.status_code == 200, r.text
    assert r.json()["restart_required"] is True
    assert "real-2" not in p.read_text(encoding="utf-8")


def test_add_rejects_bad_input(project, deps):
    _yml(project)
    c = _client(deps)
    dup = c.post("/api/control/accounts", headers=AUTH, json={"id": "paper-main", "env": "paper"})
    assert dup.status_code == 400 and "이미" in dup.json()["detail"]
    bad = c.post("/api/control/accounts", headers=AUTH, json={"id": "a b", "env": "paper"})
    assert bad.status_code == 400
    extra = c.post(
        "/api/control/accounts", headers=AUTH, json={"id": "p2", "env": "paper", "cano": "1"}
    )
    assert extra.status_code == 422


def test_remove_blocked_by_live_order_is_409(project, deps):
    _yml(project)
    with deps.conn_factory() as conn:
        conn.execute(
            "INSERT INTO orders(account_id, ticker, market, side, qty, order_type, status, mode,"
            " created_at) VALUES ('real-main','005930','KR','BUY',1,'limit','accepted','off','t')"
        )
        conn.commit()
    r = _client(deps).post(
        "/api/control/accounts/remove", headers=AUTH, json={"account_id": "real-main"}
    )
    assert r.status_code == 409 and "주문" in r.json()["detail"]
