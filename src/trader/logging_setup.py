"""로깅 설정: UTF-8 파일(자정 회전, 14일)·콘솔 핸들러. httpx/httpcore 는 WARNING 으로 올린다."""

from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_HANDLER_NAME = "trader"
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure(state_dir: Path, level: str = "INFO") -> None:
    """루트 로거에 파일·콘솔 핸들러를 붙인다. 여러 번 호출해도 핸들러가 중복되지 않는다."""
    root = logging.getLogger()
    root.setLevel(level)
    # httpx/httpcore 는 INFO 에 요청 URL(토큰·키 포함 가능)을 남기므로 억제한다.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    reset()
    log_dir = Path(state_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(_FORMAT)
    file_handler = TimedRotatingFileHandler(
        log_dir / "trader.log", when="midnight", backupCount=14, encoding="utf-8"
    )
    stream_handler = logging.StreamHandler()
    for h in (file_handler, stream_handler):
        h.set_name(_HANDLER_NAME)
        h.setFormatter(formatter)
        root.addHandler(h)


def reset() -> None:
    """이 모듈이 붙인 핸들러를 모두 제거하고 닫는다."""
    root = logging.getLogger()
    for h in [h for h in root.handlers if getattr(h, "name", None) == _HANDLER_NAME]:
        root.removeHandler(h)
        h.close()
