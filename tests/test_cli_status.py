import subprocess
import sys

from typer.testing import CliRunner

from trader.cli import app


def _todo(project):
    (project / "docs" / "TODO.md").write_text("## Phase 0 골격\n- [ ] 첫 일\n", encoding="utf-8")


def test_status_cli_writes_file(project):
    _todo(project)
    r = CliRunner().invoke(app, ["status"])
    assert r.exit_code == 0, r.stdout
    out = project / "state" / "STATUS.md"
    assert out.exists() and "Phase 0 골격: 0/1" in out.read_text(encoding="utf-8")
    assert str(out) in r.stdout
    assert (project / "state" / "trader.db").exists()


def test_status_cli_config_error_exit1(project):
    (project / "config" / "settings.yaml").write_text("foo: 1\n", encoding="utf-8")
    r = CliRunner().invoke(app, ["status"])
    assert r.exit_code == 1
    assert "foo" in r.stdout + (r.stderr or "")


def test_status_cli_utf8_korean_path(tmp_path, monkeypatch):
    root = tmp_path / "한글 경로"
    for name in ("config", "state", "docs", "secrets"):
        (root / name).mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    _todo(root)
    env = {"TRADER_HOME": str(root), "PYTHONIOENCODING": "cp949", "PATH": "", "HOME": str(tmp_path)}
    env["PATH"] = __import__("os").environ.get("PATH", "")
    r = subprocess.run(
        [sys.executable, "-m", "trader", "status"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        shell=False,
        cwd=str(root),
    )
    assert r.returncode == 0, r.stderr
    assert "저장: " in r.stdout and "한글 경로" in r.stdout
    assert "Phase 0 골격" in (root / "state" / "STATUS.md").read_text(encoding="utf-8")
