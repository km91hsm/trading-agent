from fastapi.testclient import TestClient
from helpers_web import AUTH, make_deps
from typer.testing import CliRunner

from trader.cli import app
from trader.web.app import create_app


def test_health_and_settings(client):
    r = client.get("/api/system/health")
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["version"]
    s = client.get("/api/system/settings").json()
    assert s["web"]["port"] == 8787 and "KIS_" not in str(s)
    assert s["risk"]["max_positions"] == 5 and s["llm"]["budget_usd"] == 5.0


def test_status_markdown_empty_when_missing(client):
    assert client.get("/api/system/status").json()["markdown"] == ""


def test_control_requires_token(client):
    assert client.post("/api/control/pause").status_code == 401
    bad = {"Authorization": "Bearer nope"}
    assert client.post("/api/control/pause", headers=bad).status_code == 401
    r = client.post("/api/control/pause", headers=AUTH)
    assert r.status_code == 200 and r.json()["paused"] is True


def test_control_unavailable_without_configured_token(project, conn):
    c = TestClient(create_app(make_deps(project, token=None)))
    assert c.post("/api/control/pause", headers=AUTH).status_code == 401
    assert c.post("/api/control/pause", headers={"Authorization": "Bearer "}).status_code == 401


def test_root_without_dist_shows_notice(client):
    r = client.get("/")
    assert r.status_code == 200 and "npm run build" in r.text


def test_root_serves_dist_index(project, conn, tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>SPA</html>", encoding="utf-8")
    (dist / "assets" / "a.js").write_text("console.log(1)", encoding="utf-8")
    c = TestClient(create_app(make_deps(project, dist=dist)))
    assert c.get("/").text == "<html>SPA</html>"
    assert c.get("/orders").text == "<html>SPA</html>"
    assert c.get("/assets/a.js").text == "console.log(1)"
    assert c.get("/api/nope").status_code == 404


def test_cli_web_command_registered():
    r = CliRunner().invoke(app, ["web", "--help"])
    assert r.exit_code == 0 and "port" in r.stdout


def test_non_ascii_token_is_compared_not_500(project, conn):
    # HTTP 헤더는 latin-1 이라 비ASCII 토큰은 보낼 수 없다. 설정만 비ASCII 여도 500 이 아니라 401.
    c = TestClient(create_app(make_deps(project, token="토큰")))
    assert c.post("/api/control/pause", headers={"Authorization": "Bearer abc"}).status_code == 401
    assert c.post("/api/control/pause").status_code == 401


def test_dist_root_file_and_traversal(project, conn, tmp_path):
    real = tmp_path / "real_dist"
    (real / "assets").mkdir(parents=True)
    (real / "index.html").write_text("<html>SPA</html>", encoding="utf-8")
    (real / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    link = tmp_path / "dist_link"
    link.symlink_to(real, target_is_directory=True)
    c = TestClient(create_app(make_deps(project, dist=link)))
    assert c.get("/favicon.svg").text == "<svg/>"  # 심볼릭 링크 dist 에서도 루트 파일 제공
    assert c.get("/../pyproject.toml").text == "<html>SPA</html>"
    assert c.get("/%2e%2e/pyproject.toml").text == "<html>SPA</html>"


def test_foreign_host_rejected(client):
    r = client.get("/api/system/health", headers={"Host": "evil.example"})
    assert r.status_code == 400
    assert client.get("/api/system/health", headers={"Host": "localhost:8787"}).status_code == 200


def test_hosts_for_bind_and_wildcard_allows_lan_host(project, conn):
    """--host 0.0.0.0 이면 클라이언트는 LAN IP 를 Host 로 보내므로 바인드 주소를 허용 목록에 넣는
    것으론 전부 400 이 난다. 비루프백 바인드는 모든 Host 를 허용한다(경고는 CLI 가 남긴다)."""
    from trader.cli import _hosts_for_bind

    assert _hosts_for_bind("127.0.0.1") == [] and _hosts_for_bind("localhost") == []
    assert _hosts_for_bind("0.0.0.0") == ["*"] and _hosts_for_bind("192.168.0.10") == ["*"]
    deps = make_deps(project)
    deps.extra_hosts.extend(_hosts_for_bind("0.0.0.0"))
    c = TestClient(create_app(deps))
    assert c.get("/api/system/health", headers={"Host": "192.168.0.10:8787"}).status_code == 200


def test_usage_endpoint_reports_windows_and_observed_limits(client, deps, conn, tmp_path):
    import json

    deps.settings.llm.usage_file = None  # 개발 머신의 ~/.claude/rate_limits.json 에 기대지 않는다
    r = client.get("/api/system/usage").json()
    assert r["rate_limits"] is None and r["windows"]["today"] == {"sessions": 0, "cap": 12}
    p = tmp_path / "rl.json"
    p.write_text(
        json.dumps({"at": 0, "rate_limits": {"five_hour": {"used_percentage": 3}}}),
        encoding="utf-8",
    )
    deps.settings.llm.usage_file = str(p)
    r = client.get("/api/system/usage").json()
    assert r["rate_limits"]["five_hour"]["used_pct"] == 3.0 and "/usage" in r["note"]


def test_daemon_endpoint_without_daemon_lists_schedule(client, deps):
    from trader.config import ScheduleConfig

    r = client.get("/api/system/daemon").json()
    assert r["alive"] is False and r["jobs"] == []  # schedule 없음 → 예정표 비움
    deps.schedule = ScheduleConfig.model_validate(
        {
            "kr": {
                "scan": "07:30",
                "monitor": {"start": "09:00", "end": "15:30", "interval_s": 60},
            },
            "us": {
                "scan": "21:30",
                "monitor": {"start": "23:30", "end": "06:00", "interval_s": 60},
            },
            "reflect": "16:30",
            "ml_train": {"weekday": "sat", "time": "10:00"},
        }
    )
    r = client.get("/api/system/daemon").json()
    assert any(j["id"] == "kr_scan_analyze" for j in r["jobs"]) and r["pid"] is None
