"""프로세스 간 파일 락 (OS 분기는 platform/ 에서만). 피처 스토어 쓰기처럼 데몬과 CLI·세션 도구가
같은 파일을 동시에 고칠 수 있는 곳에 쓴다. 락 파일은 비워 두고 OS 잠금만 건다."""

from __future__ import annotations

import contextlib
import logging
import sys
import time
from collections.abc import Iterator
from pathlib import Path

logger = logging.getLogger("trader.platform")


class LockTimeout(TimeoutError):
    """timeout 안에 파일 락을 얻지 못했다."""


def _try_lock(fh) -> bool:
    if sys.platform == "win32":
        import msvcrt

        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fh) -> None:
    if sys.platform == "win32":
        import msvcrt

        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl

    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


@contextlib.contextmanager
def locked(path: Path, *, timeout: float = 60.0, poll_s: float = 0.1) -> Iterator[None]:
    """`path` 에 배타 락을 잡고 블록을 돈다. timeout 안에 못 잡으면 LockTimeout."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+b")  # noqa: SIM115 - 락 수명 동안 열어 둔다
    try:
        if sys.platform == "win32":
            fh.seek(0)
            if fh.tell() == 0:
                fh.write(b"\0")  # msvcrt 는 바이트 범위를 잠근다
                fh.flush()
            fh.seek(0)
        deadline = time.monotonic() + timeout
        waited = False
        while not _try_lock(fh):
            if time.monotonic() >= deadline:
                raise LockTimeout(f"파일 락 대기 {timeout:.0f}s 초과: {path.name}")
            if not waited:
                logger.debug("파일 락 대기: %s", path.name)
                waited = True
            time.sleep(poll_s)
        try:
            yield
        finally:
            _unlock(fh)
    finally:
        fh.close()
