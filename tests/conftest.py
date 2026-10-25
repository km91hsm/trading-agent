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


@pytest.fixture(autouse=True)
def _reset_logging():
    """매 테스트 후 이 프로젝트가 붙인 로깅 핸들러·레벨을 원복한다."""
    import logging

    names = ("", "httpx", "httpcore")
    saved = {n: logging.getLogger(n).level for n in names}
    yield
    try:
        from trader import logging_setup
    except ImportError:
        return
    logging_setup.reset()
    for n, lvl in saved.items():
        logging.getLogger(n).setLevel(lvl)


def make_account(id: str, env: str, mode: str, markets: list[str] | None = None):
    from trader.config import AccountConfig

    return AccountConfig(id=id, env=env, cano="00000000", markets=markets or ["KR"], mode=mode)


# --- 웹 API (Phase 5). tests/web/conftest.py 를 두면 루트 `conftest` 모듈과 이름이 겹친다 ---


@pytest.fixture
def deps(project, conn):
    from helpers_web import make_deps

    return make_deps(project)


@pytest.fixture
def client(deps):
    from fastapi.testclient import TestClient

    from trader.web.app import create_app

    return TestClient(create_app(deps))


@pytest.fixture(autouse=True)
def _clear_macro_memo():
    """거시 수집 메모(inputs._MACRO_MEMO)는 프로세스 전역이라 테스트 사이에 비운다."""
    from trader.sources import inputs as _inputs

    _inputs._MACRO_MEMO.clear()
    yield
    _inputs._MACRO_MEMO.clear()
