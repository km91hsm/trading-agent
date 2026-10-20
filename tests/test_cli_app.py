"""번들 첫 실행: 템플릿으로 config/·secrets/ 를 채우고(기존 불변), 더블클릭은 데몬+웹+브라우저."""

import sys

from typer.testing import CliRunner

from trader import cli
from trader.cli import app
from trader.platform import bundle


def test_first_run_init_copies_missing_only(project):
    (project / "config" / "settings.yaml").write_text("llm: {budget_usd: 9}\n", encoding="utf-8")
    created = bundle.first_run_init(project)
    kept = (project / "config" / "settings.yaml").read_text(encoding="utf-8")
    assert kept == "llm: {budget_usd: 9}\n"
    assert "config/settings.yaml" not in created and "config/accounts.yaml" in created
    assert "00000000" in (project / "config" / "accounts.yaml").read_text(encoding="utf-8")
    assert (project / "secrets" / ".env.example").exists()
    assert not (project / "secrets" / ".env").exists()  # 키는 사용자가 넣는다
    for d in ("state", "docs"):
        assert (project / d).is_dir()
    assert bundle.first_run_init(project) == []  # 두 번째는 할 일 없음


def test_cli_init_prints_created(project):
    r = CliRunner().invoke(app, ["init"])
    assert r.exit_code == 0, r.stdout
    assert "config/accounts.yaml" in r.stdout and "secrets/.env.example" in r.stdout


def test_app_runs_daemon_and_opens_browser(project, monkeypatch):
    import yaml

    bundle.first_run_init(project)
    acct = {"id": "paper-main", "env": "paper", "cano": "0", "markets": ["KR"], "mode": "auto"}
    (project / "config" / "accounts.yaml").write_text(
        yaml.safe_dump({"accounts": [acct]}), encoding="utf-8"
    )
    seen = {}
    monkeypatch.setattr(cli, "_build_market", lambda s: None)
    monkeypatch.setattr(cli, "_build_brokers", lambda s, a: [])
    import threading

    opened = threading.Event()

    def fake_run(deps, **kw):
        seen.update(web=kw.get("web"), deps=deps)
        opened.wait(3)  # 실제 데몬처럼 웹이 뜬 뒤에도 살아 있다
        return {"ok": True}

    def fake_open(url):
        seen["url"] = url
        opened.set()
        return True

    monkeypatch.setattr("trader.daemon.run", fake_run)
    monkeypatch.setattr(cli, "_wait_for_port", lambda *a, **kw: True)  # 포트가 열린 것으로
    monkeypatch.setattr("webbrowser.open", fake_open)
    r = CliRunner().invoke(app, ["app", "--browser-delay", "0"])
    assert r.exit_code == 0, r.stdout
    assert seen["web"] is True and seen["url"].startswith("http://127.0.0.1:8787")


def test_main_double_click_routes_to_app(monkeypatch):
    calls = []
    monkeypatch.setattr(bundle, "is_frozen", lambda: True)
    monkeypatch.setattr(sys, "argv", ["trader"])
    monkeypatch.setattr(cli, "app", lambda args=None: calls.append(args))
    from trader import __main__ as entry

    entry.main()
    assert calls == [["app"]]
    monkeypatch.setattr(sys, "argv", ["trader", "status"])
    entry.main()
    assert calls[-1] is None  # 인자가 있으면 보통 CLI


def test_main_double_click_pauses_on_error_in_windows_console(monkeypatch):
    """Windows 더블클릭은 오류로 끝나면 콘솔이 바로 닫혀 메시지를 못 본다 → 종료 전 Enter 대기."""
    import typer

    from trader import __main__ as entry

    monkeypatch.setattr(bundle, "is_frozen", lambda: True)
    monkeypatch.setattr(sys, "argv", ["trader"])
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)

    def failing_app(args=None):
        raise typer.Exit(code=1)

    waited = []
    monkeypatch.setattr(cli, "app", failing_app)
    monkeypatch.setattr("builtins.input", lambda prompt="": waited.append(prompt) or "")
    try:
        entry.main()
    except SystemExit as e:
        assert e.code == 1
    assert waited and "Enter" in waited[0]


def test_wait_for_port_returns_when_listening_and_gives_up(monkeypatch):
    import socket

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert cli._wait_for_port("127.0.0.1", port, timeout_s=2.0) is True
    finally:
        srv.close()
    assert cli._wait_for_port("127.0.0.1", port, timeout_s=0.3) is False


def test_selfcheck_imports_heavy_stacks(project):
    """번들 스모크용: 무거운 라이브러리(sklearn·pyarrow·pandas·fastapi·uvicorn·apscheduler)가 실제로
    import 되는지 JSON 으로 알린다 (--help·status 만으로는 hidden import 누락을 못 잡는다)."""
    import json

    r = CliRunner().invoke(app, ["selfcheck"])
    assert r.exit_code == 0, r.stdout
    data = json.loads(r.stdout.strip().splitlines()[-1])
    assert data["ok"] is True
    heavy = {"sklearn", "pyarrow", "pandas", "fastapi", "uvicorn", "apscheduler"}
    assert heavy <= set(data["modules"])


def test_templates_mirror_config_except_account_numbers(project):
    """CLAUDE.md 규칙: templates/config/*.yaml 은 config/*.yaml 사본(계좌번호만 비움)."""
    import re
    from pathlib import Path

    import yaml

    repo = Path(__file__).resolve().parents[1]
    for src in sorted((repo / "config").glob("*.yaml")):
        a = yaml.safe_load(src.read_text(encoding="utf-8"))
        b = yaml.safe_load((repo / "templates" / "config" / src.name).read_text(encoding="utf-8"))
        if src.name == "accounts.yaml":
            for acct in a["accounts"]:
                acct["cano"] = "00000000"
        if src.name == "sources.yaml":
            a["edgar"]["user_agent"] = "trading-agent <your-email@example.com>"
        assert a == b, src.name
    tmpl_sources = (repo / "templates" / "config" / "sources.yaml").read_text(encoding="utf-8")
    assert not re.search(r"[A-Za-z0-9._%+-]+@gmail\.com", tmpl_sources)  # 개인 이메일 금지


def test_selfcheck_reports_optional_modules(project):
    """선택 백엔드(lightgbm·stable_baselines3)는 있으면 true, 없어도 ok 에 영향이 없다."""
    import json

    r = CliRunner().invoke(app, ["selfcheck"])
    assert r.exit_code == 0, r.stdout
    data = json.loads(r.stdout.strip().splitlines()[-1])
    assert set(data["optional"]) == {"lightgbm", "stable_baselines3"}
    assert all(isinstance(v, bool) for v in data["optional"].values())
    assert data["ok"] is True
