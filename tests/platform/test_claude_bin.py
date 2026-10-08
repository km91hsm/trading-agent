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
    with pytest.raises(claude_bin.ClaudeNotFound):
        claude_bin.find_claude()
