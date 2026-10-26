"""SaveTicker의 사용자 승인 브라우저 수집. 거래 실행과 독립이다.

저장 위치는 리더(`trader.sources.saveticker`)가 읽는 `sources.yaml news.saveticker_dir` 와 같다 —
수집기와 리더가 같은 폴더를 보고, 같은 폴더를 쓰는 두 서비스(예: launchd 와 데몬)는 그 폴더의
`runtime/service.lock` 으로 하나만 돈다.
"""

from __future__ import annotations

from pathlib import Path

from trader.platform import paths


def home() -> Path:
    """수집 폴더(`data/`·`runtime/` 의 부모). 설정을 읽지 못하면 기본 폴더."""
    from trader import config
    from trader.sources import saveticker as reader

    try:
        configured = config.load_sources().news.saveticker_dir
    except config.ConfigError:
        configured = reader.DEFAULT_DIR
    return reader.resolve_dir(configured) or paths.state_dir() / "external" / "saveticker"


def credentials_path() -> Path:
    """계정 파일(`SAVETICKER_EMAIL`·`SAVETICKER_PASSWORD`). 값은 수집 프로세스만 읽는다."""
    return paths.project_root() / "secrets" / "saveticker.env"
