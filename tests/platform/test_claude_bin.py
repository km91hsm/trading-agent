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
    monkeypatch.setattr(claude_bin, "_known_locations", lambda: [tmp_path / "nope", exe])
    assert claude_bin.find_claude() == exe


def test_known_locations_include_user_install(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    locs = [str(p) for p in claude_bin._known_locations()]
    assert str(tmp_path / ".local" / "bin" / "claude") in locs or any(
        "claude.exe" in p for p in locs
    )
