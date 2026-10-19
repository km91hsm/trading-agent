"""번들 첫 실행: 템플릿으로 config/·secrets/ 를 채우고(있는 파일 불변), 더블클릭은 데몬+웹+브라우저."""

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
    def fake_run(deps, **kw):
        seen.update(web=kw.get("web"), deps=deps)
        return {"ok": True}

    monkeypatch.setattr("trader.daemon.run", fake_run)
    monkeypatch.setattr("webbrowser.open", lambda url: seen.update(url=url) or True)
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
