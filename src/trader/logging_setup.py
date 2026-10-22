"""로깅 설정: UTF-8 파일(자정 회전, 14일)·콘솔 핸들러. httpx/httpcore 는 WARNING 으로 올린다."""

from __future__ import annotations

import logging
import os
import sys
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_HANDLER_NAME = "trader"
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class SharedTimedRotatingFileHandler(TimedRotatingFileHandler):
    """여러 프로세스(데몬·web·telegram loop)가 같은 파일을 쓸 때용.

    표준 구현은 보관 파일이 이미 있으면(다른 프로세스가 먼저 회전) 스트림을 다시 열지 않고 돌아가
    renamed 된 옛 파일에 계속 쓰고(14일 뒤 삭제돼 로그 유실), 기록마다 회전 검사를 반복한다."""

    def doRollover(self) -> None:  # noqa: N802 - stdlib 이름
        t = self.rolloverAt - self.interval
        time_tuple = time.gmtime(t) if self.utc else time.localtime(t)
        suffix = time.strftime(self.suffix, time_tuple)
        dfn = self.rotation_filename(f"{self.baseFilename}.{suffix}")
        if os.path.exists(dfn):
            if self.stream:
                self.stream.close()
                self.stream = None
            if not self.delay:
                self.stream = self._open()
            self.rolloverAt = self.computeRollover(int(time.time()))
            return
        try:
            super().doRollover()
        except PermissionError:
            # Windows: 다른 프로세스(데몬)가 파일을 쥐고 있으면 rename 이 WinError 32
            # → 회전을 건너뛰고 같은 파일에 계속 쓴다. 다음 자정에 다시 시도한다
            # (handleError 로 stderr 소음을 내지 않음).
            if self.stream is None and not self.delay:
                self.stream = self._open()
            self.rolloverAt = self.computeRollover(int(time.time()))


def file_handler(path: Path) -> SharedTimedRotatingFileHandler:
    """자정 회전·14일 보관 파일 핸들러 (프로세스 간 공유 안전)."""
    return SharedTimedRotatingFileHandler(path, when="midnight", backupCount=14, encoding="utf-8")


def quiet_httpx() -> None:
    """httpx/httpcore 는 INFO 에 요청 URL(토큰·키 포함 가능)을 남기므로 WARNING 으로 올린다.

    configure() 가 부르고, configure 없이 쓰이는 클라이언트(테스트 등)도 생성자에서 부른다."""
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def configure(state_dir: Path, level: str = "INFO", *, name: str | None = None) -> None:
    """루트 로거에 파일·콘솔 핸들러를 붙인다. 여러 번 호출해도 핸들러가 중복되지 않는다.

    name 을 주면 `trader-<name>.log` 에 쓴다 — 데몬과 함께 오래 사는 명령(web·telegram loop·monitor)
    이 같은 파일을 두고 회전·뒤섞임을 겪지 않도록. 짧은 CLI 명령은 기본 trader.log 를 쓴다."""
    root = logging.getLogger()
    root.setLevel(level)
    quiet_httpx()
    reset()
    log_dir = Path(state_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(_FORMAT)
    fname = f"trader-{name}.log" if name else "trader.log"
    handlers: list[logging.Handler] = [file_handler(log_dir / fname)]
    if sys.stderr is not None:  # 창 없는 실행파일(TraderApp)은 stderr 가 None — 파일 핸들러만
        handlers.append(logging.StreamHandler())
    for h in handlers:
        h.set_name(_HANDLER_NAME)
        h.setFormatter(formatter)
        root.addHandler(h)


def reset() -> None:
    """이 모듈이 붙인 핸들러를 모두 제거하고 닫는다."""
    root = logging.getLogger()
    for h in [h for h in root.handlers if getattr(h, "name", None) == _HANDLER_NAME]:
        root.removeHandler(h)
        h.close()
