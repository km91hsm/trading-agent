"""번들(PyInstaller frozen) 분기: 루트·리소스·도구 명령은 platform/bundle 한 곳이 답한다."""

import sys

from trader.platform import bundle, paths


def _freeze(monkeypatch, exe_dir):
    (exe_dir / "_internal").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "trader"))
    monkeypatch.setattr(sys, "_MEIPASS", str(exe_dir / "_internal"), raising=False)
    monkeypatch.delenv("TRADER_HOME", raising=False)


def test_not_frozen_defaults(project):
    assert bundle.is_frozen() is False
    assert bundle.tool_command() == "python -m trader tool"
    assert bundle.tool_bin_dir() == paths.Path(sys.executable).parent
    assert paths.web_dist() == project / "web" / "dist"
    assert paths.agents_dir() == project / ".claude" / "agents"
    assert bundle.daemon_argv() == [sys.executable, "-m", "trader", "daemon"]


def test_frozen_root_is_executable_dir(tmp_path, monkeypatch):
    """pyproject 가 없는 아무 폴더에 풀어도 실행파일 폴더가 TRADER_HOME 이다 (폴더째 이동 가능)."""
    exe_dir = tmp_path / "dist" / "trader"
    _freeze(monkeypatch, exe_dir)
    monkeypatch.chdir(tmp_path)  # cwd 는 상관없다
    assert bundle.is_frozen() is True
    assert paths.project_root() == exe_dir.resolve()
    assert bundle.tool_command() == "trader tool"
    assert bundle.tool_bin_dir() == exe_dir.resolve()
    assert paths.web_dist() == exe_dir / "_internal" / "web" / "dist"
    assert paths.agents_dir() == exe_dir / "_internal" / "agents"
    assert bundle.daemon_argv() == [str(exe_dir / "trader"), "daemon"]


def test_trader_home_still_wins_when_frozen(tmp_path, monkeypatch):
    exe_dir = tmp_path / "dist" / "trader"
    _freeze(monkeypatch, exe_dir)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("TRADER_HOME", str(home))
    assert paths.project_root() == home.resolve()
