from pathlib import Path

import pytest

from trader.platform import paths


def test_trader_home_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADER_HOME", str(tmp_path))
    assert paths.project_root() == tmp_path
    assert paths.config_dir() == tmp_path / "config"
    assert paths.secrets_path() == tmp_path / "secrets" / ".env"
    assert paths.docs_dir() == tmp_path / "docs"


def test_search_up_for_pyproject(tmp_path, monkeypatch):
    monkeypatch.delenv("TRADER_HOME", raising=False)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert paths.project_root() == tmp_path.resolve()


def test_state_dir_is_created(project: Path):
    (project / "state").rmdir()
    assert paths.state_dir().is_dir()


def test_trader_home_missing_dir_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADER_HOME", str(tmp_path / "오타"))
    with pytest.raises(paths.ProjectRootNotFound, match="TRADER_HOME"):
        paths.project_root()
    assert not (tmp_path / "오타").exists()


def test_root_not_found(tmp_path, monkeypatch):
    monkeypatch.delenv("TRADER_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(paths.ProjectRootNotFound):
        paths.project_root()
