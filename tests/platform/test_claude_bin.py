import shutil
from pathlib import Path

import pytest

from trader.platform import claude_bin


def test_find_claude_override_missing_raises(tmp_path):
    with pytest.raises(claude_bin.ClaudeNotFound):
        claude_bin.find_claude(str(tmp_path / "없음"))


def test_find_claude_override_existing(tmp_path):
    exe = tmp_path / "claude"
    exe.write_text("", encoding="utf-8")
    assert claude_bin.find_claude(str(exe)) == exe


def test_find_claude_on_path(monkeypatch, tmp_path):
    found = tmp_path / "claude"
    monkeypatch.setattr(
        shutil, "which", lambda name, **kw: str(found) if name == "claude" else None
    )
    assert claude_bin.find_claude() == Path(found)


def test_find_claude_not_on_path(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name, **kw: None)
    monkeypatch.setattr(claude_bin, "_known_locations", lambda: [])
    with pytest.raises(claude_bin.ClaudeNotFound):
        claude_bin.find_claude()


def test_find_claude_in_known_location_when_path_is_minimal(monkeypatch, tmp_path):
    """Finder·로그인 항목으로 띄운 앱은 PATH 가 /usr/bin:/bin 뿐이라 ~/.local/bin 이 빠진다 —
    알려진 설치 위치(공식 설치 스크립트·Homebrew·npm 전역)를 더 본다."""
    monkeypatch.setattr(shutil, "which", lambda name, **kw: None)
    exe = tmp_path / ".local" / "bin" / "claude"
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setattr(claude_bin, "_known_locations", lambda: [tmp_path / "nope", exe])
    assert claude_bin.find_claude() == exe


def test_known_locations_include_user_install(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    locs = [str(p) for p in claude_bin._known_locations()]
    assert str(tmp_path / ".local" / "bin" / "claude") in locs or any(
        "claude.exe" in p for p in locs
    )


def test_skips_non_executable_candidate(monkeypatch, tmp_path):
    import os
    import sys

    if sys.platform.startswith("win"):
        pytest.skip("실행 비트는 POSIX 만")
    monkeypatch.setattr(shutil, "which", lambda name, **kw: None)
    bad, good = tmp_path / "a" / "claude", tmp_path / "b" / "claude"
    for p, mode in ((bad, 0o644), (good, 0o755)):
        p.parent.mkdir()
        p.write_text("", encoding="utf-8")
        os.chmod(p, mode)
    monkeypatch.setattr(claude_bin, "_known_locations", lambda: [bad, good])
    assert claude_bin.find_claude() == good


def test_child_path_adds_claude_dir_and_brew(tmp_path):
    import os

    p = claude_bin.child_path(tmp_path / "bin" / "claude", "/usr/bin:/bin")
    parts = p.split(os.pathsep)
    assert parts[:2] == ["/usr/bin", "/bin"] and str(tmp_path / "bin") in parts
