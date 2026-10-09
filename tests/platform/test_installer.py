from pathlib import Path

import pytest
from typer.testing import CliRunner

from trader.cli import app
from trader.config import ConfigError
from trader.platform import installer


def test_render_plist_contains_python_and_cwd(tmp_path):
    text = installer.render("darwin", python=Path("/venv/bin/python"), project_root=tmp_path)
    assert "<string>/venv/bin/python</string>" in text and "<string>-m</string>" in text
    assert "<string>trader</string>" in text and "<string>daemon</string>" in text
    assert f"<string>{tmp_path}</string>" in text and "RunAtLoad" in text and "KeepAlive" in text
    assert str(tmp_path / "state" / "logs" / "daemon.out") in text
    assert "com.trader.daemon" in text


def test_render_schtasks_xml_logon_trigger(tmp_path):
    text = installer.render(
        "win32", python=Path(r"C:\venv\Scripts\python.exe"), project_root=tmp_path
    )
    assert "<LogonTrigger>" in text and "<Command>C:\\venv\\Scripts\\python.exe</Command>" in text
    assert "<Arguments>-m trader daemon</Arguments>" in text
    assert f"<WorkingDirectory>{tmp_path}</WorkingDirectory>" in text


def test_install_dry_run_writes_nothing_and_prints_command(tmp_path):
    calls = []
    r = installer.install(
        dry_run=True,
        run=lambda cmd, **kw: calls.append(cmd),
        platform="darwin",
        home=tmp_path,
        python=Path("/venv/bin/python"),
        project_root=tmp_path / "proj",
    )
    assert r["written"] is False and calls == []
    assert r["path"] == tmp_path / "Library" / "LaunchAgents" / "com.trader.daemon.plist"
    assert r["command"][:2] == ["launchctl", "load"]


def test_install_darwin_writes_and_calls_launchctl(tmp_path):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    r = installer.install(
        dry_run=False,
        run=run,
        platform="darwin",
        home=tmp_path,
        python=Path("/venv/bin/python"),
        project_root=tmp_path / "proj",
    )
    assert r["written"] is True and r["path"].exists()
    assert calls == [["launchctl", "load", "-w", str(r["path"])]]
    installer.uninstall(run=run, platform="darwin", home=tmp_path)
    assert calls[-1] == ["launchctl", "unload", "-w", str(r["path"])] and not r["path"].exists()


def test_uninstall_win32_calls_schtasks_delete(tmp_path):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    r = installer.install(
        dry_run=False,
        run=run,
        platform="win32",
        home=tmp_path,
        python=Path(r"C:\venv\Scripts\python.exe"),
        project_root=tmp_path / "proj",
    )
    assert (
        calls[0][:3] == ["schtasks", "/Create", "/TN"] and "/XML" in calls[0] and "/F" in calls[0]
    )
    assert r["path"].suffix == ".xml"
    installer.uninstall(run=run, platform="win32", home=tmp_path)
    assert calls[-1] == ["schtasks", "/Delete", "/TN", "trader-daemon", "/F"]


def test_unsupported_platform_rejected(tmp_path):
    with pytest.raises(ConfigError):
        installer.install(
            dry_run=True, platform="linux", home=tmp_path, python=Path("/p"), project_root=tmp_path
        )


def test_install_failure_is_error(tmp_path):
    def run(cmd, **kw):
        class R:
            returncode = 1
            stdout = ""
            stderr = "boom"

        return R()

    with pytest.raises(ConfigError, match="boom"):
        installer.install(
            dry_run=False,
            run=run,
            platform="darwin",
            home=tmp_path,
            python=Path("/p"),
            project_root=tmp_path,
        )


def test_cli_install_dry_run(project):
    r = CliRunner().invoke(app, ["install", "--dry-run"])
    assert r.exit_code == 0, r.stdout
    assert "launchctl" in r.stdout or "schtasks" in r.stdout
