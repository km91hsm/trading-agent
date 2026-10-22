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


def test_cli_executable_prefers_console_binary(tmp_path, monkeypatch):
    """창 없는 TraderApp 으로 떠도 세션의 `trader tool`·launchd 의 `trader daemon` 은
    콘솔 CLI 를 쓴다."""
    exe_dir = tmp_path / "dist" / "trader"
    _freeze(monkeypatch, exe_dir)
    (exe_dir / "trader").write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(exe_dir / "TraderApp"))
    assert bundle.executable() == (exe_dir / "TraderApp").resolve()
    assert bundle.cli_executable() == (exe_dir / "trader").resolve()
    assert bundle.tool_bin_dir() == exe_dir.resolve()
    assert bundle.daemon_argv() == [str((exe_dir / "trader").resolve()), "daemon"]


def test_cli_executable_falls_back_to_self(tmp_path, monkeypatch):
    exe_dir = tmp_path / "dist" / "trader"
    _freeze(monkeypatch, exe_dir)
    assert bundle.cli_executable() == bundle.executable()


def test_spec_exe_names_differ_case_insensitively():
    """Windows 는 대소문자를 무시하므로 Trader.exe/trader.exe 를 같이 둘 수 없다."""
    import re

    spec_path = paths.Path(__file__).resolve().parents[2] / "packaging" / "trader.spec"
    spec = spec_path.read_text(encoding="utf-8")
    names = re.findall(r'EXE\((?:.|\n)*?name="([^"]+)"', spec)
    assert len(names) == 2, names
    assert names[0].lower() != names[1].lower()
    assert "trader" in names and "TraderApp" in names


def test_bundle_root_outside_app_bundle(tmp_path, monkeypatch):
    """macOS .app: TRADER_HOME 은 .app 의 부모 폴더(config/secrets/state 가 .app 옆), 리소스는
    Contents/Frameworks(sys._MEIPASS), 도구 명령 디렉터리는 Contents/MacOS."""
    root = tmp_path / "x" / "trader"
    macos = root / "Trader.app" / "Contents" / "MacOS"
    frameworks = root / "Trader.app" / "Contents" / "Frameworks"
    macos.mkdir(parents=True)
    frameworks.mkdir(parents=True)
    (macos / "trader").write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(macos / "TraderApp"))
    monkeypatch.setattr(sys, "_MEIPASS", str(frameworks), raising=False)
    monkeypatch.delenv("TRADER_HOME", raising=False)
    assert bundle.app_bundle_dir() == (root / "Trader.app").resolve()
    assert bundle.bundle_root() == root.resolve()
    assert paths.project_root() == root.resolve()
    assert bundle.resource_dir() == frameworks.resolve()
    assert bundle.tool_bin_dir() == macos.resolve()
    assert bundle.cli_executable() == (macos / "trader").resolve()
    assert bundle.icons_dir() == frameworks.resolve() / "icons"


def test_bundle_root_windows_flat_layout_unchanged(tmp_path, monkeypatch):
    exe_dir = tmp_path / "dist" / "trader"
    _freeze(monkeypatch, exe_dir)
    assert bundle.app_bundle_dir() is None
    assert bundle.bundle_root() == exe_dir.resolve()
