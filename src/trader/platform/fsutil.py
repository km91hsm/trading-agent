"""파일 교체 헬퍼 (OS 차이는 platform/ 에서만).

Windows 는 다른 프로세스가 대상 파일을 열고 있으면 `os.replace` 가 PermissionError(공유 위반)로
실패한다 — 데몬이 날짜 파일·체크포인트를 쓰는 순간 웹·CLI 가 읽고 있으면 쓰기가 통째로 날아갔다
(전체 검토 P11). 잠깐 기다렸다 다시 시도한다. POSIX 에서는 첫 시도로 끝난다.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

REPLACE_ATTEMPTS = 6
REPLACE_DELAY_S = 0.05


def atomic_replace(
    src: Path | str,
    dst: Path | str,
    *,
    attempts: int = REPLACE_ATTEMPTS,
    delay_s: float = REPLACE_DELAY_S,
) -> None:
    """`os.replace(src, dst)` 를 PermissionError 에 한해 짧게 물러서며 다시 시도한다(최대 attempts).
    마지막에도 실패하면 그 예외를 올린다."""
    for i in range(max(1, attempts)):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay_s * (2**i))
