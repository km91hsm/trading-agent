"""프로젝트 루트와 주요 디렉터리 경로."""

from __future__ import annotations

import os
from pathlib import Path


class ProjectRootNotFound(RuntimeError):
    """프로젝트 루트를 찾지 못했을 때 발생."""


def project_root() -> Path:
    """TRADER_HOME 을 우선 쓰고, 없으면 cwd 에서 위로 올라가며 pyproject.toml 을 찾는다."""
    env = os.environ.get("TRADER_HOME")
    if env:
        home = Path(env)
        if not home.is_dir():
            raise ProjectRootNotFound(f"TRADER_HOME 이 가리키는 디렉터리가 없습니다: {env}")
        return home
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise ProjectRootNotFound(
        "프로젝트 루트를 찾을 수 없습니다. TRADER_HOME 을 설정하거나 "
        "pyproject.toml 이 있는 디렉터리 아래에서 실행하세요."
    )


def config_dir() -> Path:
    return project_root() / "config"


def state_dir() -> Path:
    path = project_root() / "state"
    path.mkdir(parents=True, exist_ok=True)
    return path


def secrets_path() -> Path:
    return project_root() / "secrets" / ".env"


def docs_dir() -> Path:
    return project_root() / "docs"
