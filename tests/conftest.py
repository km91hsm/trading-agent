from pathlib import Path

import pytest


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """임시 프로젝트 루트를 만들고 TRADER_HOME 과 cwd 를 그곳으로 옮긴다."""
    for name in ("config", "state", "docs", "secrets"):
        (tmp_path / name).mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    monkeypatch.setenv("TRADER_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def conn(project: Path):
    """초기화된 원장 DB 연결."""
    from trader.ledger import db

    c = db.connect()
    db.init_schema(c)
    yield c
    c.close()


def make_account(id: str, env: str, mode: str, markets: list[str] | None = None):
    from trader.config import AccountConfig

    return AccountConfig(id=id, env=env, cano="00000000", markets=markets or ["KR"], mode=mode)
