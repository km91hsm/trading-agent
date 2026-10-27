"""설정 화면의 연결 키 입력: 값은 돌려주지 않고 '설정됨' 여부만, 토큰 + 이 PC 직접 접속에서만,
목록 밖 이름·여러 줄 값 거부, 바꾸기 전 백업. 웹 토큰은 이 PC 같은 출처 화면에만 내준다."""

from fastapi.testclient import TestClient
from helpers_web import AUTH, TOKEN

from trader import secrets_io
from trader.web.app import create_app

LOCAL = ("127.0.0.1", 50000)


def _client(deps):
    return TestClient(create_app(deps), client=LOCAL, base_url="http://127.0.0.1")


def _env(project):
    return project / "secrets" / ".env"


def test_status_shows_set_flags_without_values(project, deps):
    _env(project).write_text("KIS_REAL_APP_KEY=topsecret\n# 메모\n", encoding="utf-8")
    c = _client(deps)
    r = c.post("/api/control/secrets/status", headers=AUTH)
    assert r.status_code == 200
    assert "topsecret" not in r.text
    keys = {k["name"]: k for k in r.json()["keys"]}
    assert keys["KIS_REAL_APP_KEY"]["set"] is True and keys["KIS_HTS_ID"]["set"] is False
    # 계좌마다 전용 키 묶음(real-main → KIS_REAL_MAIN_*)
    assert keys["KIS_REAL_MAIN_APP_KEY"]["account"] == "real-main"
    uses = {a["id"]: a["uses"] for a in r.json()["accounts"]}
    assert uses == {"paper-main": "없음", "real-main": "공용"}


def test_update_writes_backup_and_clears(project, deps):
    _env(project).write_text("# 메모\nKIS_HTS_ID=old\nTELEGRAM_CHAT_ID=1\n", encoding="utf-8")
    c = _client(deps)
    r = c.post(
        "/api/control/secrets",
        headers=AUTH,
        json={
            "values": {"KIS_HTS_ID": "newid", "KIS_REAL_MAIN_APP_KEY": "k"},
            "clear": ["TELEGRAM_CHAT_ID"],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["updated"] == ["KIS_HTS_ID", "KIS_REAL_MAIN_APP_KEY"] and body["restart_required"]
    assert "newid" not in r.text
    text = _env(project).read_text(encoding="utf-8")
    assert "# 메모" in text and "KIS_HTS_ID=newid" in text and "TELEGRAM_CHAT_ID" not in text
    assert (project / "secrets" / body["backup"]).read_text(encoding="utf-8").count("old") == 1


def test_update_rejects_unknown_names_and_multiline(project, deps):
    c = _client(deps)
    bad = c.post("/api/control/secrets", headers=AUTH, json={"values": {"PATH": "/x"}})
    assert bad.status_code == 400
    nl = c.post(
        "/api/control/secrets", headers=AUTH, json={"values": {"KIS_HTS_ID": "a\nWEB_TOKEN=x"}}
    )
    assert nl.status_code == 400
    sp = c.post("/api/control/secrets", headers=AUTH, json={"values": {"KIS_HTS_ID": "a b"}})
    assert sp.status_code == 400
    interp = c.post(
        "/api/control/secrets", headers=AUTH, json={"values": {"KIS_HTS_ID": "${WEB_TOKEN}"}}
    )
    assert interp.status_code == 400
    assert not _env(project).exists()


def test_requires_token_and_direct_local(project, deps):
    c = _client(deps)
    assert c.post("/api/control/secrets/status").status_code == 401
    proxied = c.post(
        "/api/control/secrets/status",
        headers={**AUTH, "Tailscale-User-Login": "me@example.com", "X-Forwarded-For": "100.1.2.3"},
    )
    assert proxied.status_code == 403
    remote = TestClient(create_app(deps), client=("100.64.0.9", 1), base_url="http://127.0.0.1")
    assert remote.post("/api/control/secrets/status", headers=AUTH).status_code == 403


def test_bootstrap_token_same_origin_only(project, deps):
    c = _client(deps)
    assert c.get("/api/control/bootstrap-token").status_code == 403
    cross = c.get("/api/control/bootstrap-token", headers={"Sec-Fetch-Site": "cross-site"})
    assert cross.status_code == 403
    ok = c.get("/api/control/bootstrap-token", headers={"Sec-Fetch-Site": "same-origin"})
    assert ok.status_code == 200 and ok.json() == {"token": TOKEN}


def test_ensure_web_token_creates_once(project):
    p = _env(project)
    t1, made1 = secrets_io.ensure_web_token(p)
    t2, made2 = secrets_io.ensure_web_token(p)
    assert made1 and not made2 and t1 == t2 and len(t1) >= 24
    assert f"WEB_TOKEN={t1}" in p.read_text(encoding="utf-8")


def test_status_reflects_env_overrides(project, deps, monkeypatch):
    """Codex(PR #48): 상태는 실제 로더와 같게(.env + 환경변수), 환경변수가 덮는 칸은 표시."""
    monkeypatch.setenv("KIS_REAL_MAIN_APP_KEY", "from-env")
    monkeypatch.setenv("KIS_REAL_MAIN_APP_SECRET", "from-env-s")
    c = _client(deps)
    r = c.post("/api/control/secrets/status", headers=AUTH).json()
    keys = {k["name"]: k for k in r["keys"]}
    assert keys["KIS_REAL_MAIN_APP_KEY"]["set"] and keys["KIS_REAL_MAIN_APP_KEY"]["env"]
    assert not keys["KIS_HTS_ID"]["env"]
    assert {a["id"]: a["uses"] for a in r["accounts"]}["real-main"] == "전용"
    w = c.post(
        "/api/control/secrets", headers=AUTH, json={"values": {"KIS_REAL_MAIN_APP_KEY": "x"}}
    )
    assert w.json()["env_overridden"] == ["KIS_REAL_MAIN_APP_KEY"]


def test_concurrent_updates_keep_both(project):
    import threading

    from helpers_web import _accounts

    p = _env(project)
    names = ["TELEGRAM_CHAT_ID", "DART_API_KEY", "FRED_API_KEY", "ECOS_API_KEY", "REDDIT_USER"]
    errors = []

    def put(name):
        try:
            secrets_io.update(p, _accounts(), values={name: "v" + name[:3]})
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=put, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    text = p.read_text(encoding="utf-8")
    assert not errors and all(f"{n}=" in text for n in names)


def test_testserver_host_is_not_trusted_in_production(project):
    """DNS 리바인딩: 운영 앱은 Host `testserver` 를 받지 않는다(테스트 픽스처만 허용)."""
    from dataclasses import replace

    from helpers_web import make_deps

    d = replace(make_deps(project), extra_hosts=[])
    c = TestClient(create_app(d), client=LOCAL)  # 기본 Host 가 testserver
    assert (
        c.get("/api/control/bootstrap-token", headers={"Sec-Fetch-Site": "same-origin"}).status_code
        == 400
    )


def test_ensure_fresh_rejects_cross_site(project, deps):
    c = _client(deps)
    r = c.post("/api/portfolio/ensure-fresh?market=KR", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
