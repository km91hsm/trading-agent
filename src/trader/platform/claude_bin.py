"""claude 실행파일 탐색."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


class ClaudeNotFound(RuntimeError):
    """claude 실행파일을 찾지 못했을 때 발생."""


def find_claude(override: str | None = None) -> Path:
    """override 경로가 있으면 그것을 쓰고, 없으면 PATH 에서 찾는다 (Windows 는 .cmd/.exe 우선)."""
    if override:
        path = Path(override)
        if not path.exists():
            raise ClaudeNotFound(f"지정한 claude 경로가 없습니다: {override}")
        return path
    if sys.platform.startswith("win"):
        names = ("claude.cmd", "claude.exe", "claude")
    else:
        names = ("claude", "claude.cmd", "claude.exe")
    for name in names:
        found = shutil.which(name, mode=os.F_OK)
        if found:
            return Path(found)
    raise ClaudeNotFound(
        "PATH 에서 claude 실행파일을 찾을 수 없습니다. Claude Code 를 설치하거나 "
        "settings.yaml 의 claude_bin 에 경로를 지정하세요."
    )
