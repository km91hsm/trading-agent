"""claude 실행파일 탐색."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


class ClaudeNotFound(RuntimeError):
    """claude 실행파일을 찾지 못했을 때 발생."""


def _known_locations() -> list[Path]:
    """PATH 밖의 흔한 설치 위치. Finder·로그인 항목·launchd 로 띄운 앱(Trader.app)은 PATH 가
    `/usr/bin:/bin:/usr/sbin:/sbin` 뿐이라 공식 설치 스크립트의 `~/.local/bin` 을 못 본다."""
    home = Path.home()
    if sys.platform.startswith("win"):
        appdata = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        return [
            home / ".local" / "bin" / "claude.exe",
            appdata / "npm" / "claude.cmd",
            home / ".claude" / "local" / "claude.exe",
        ]
    return [
        home / ".local" / "bin" / "claude",  # 공식 설치 스크립트(네이티브)
        home / ".claude" / "local" / "claude",  # 예전 로컬 설치
        Path("/opt/homebrew/bin/claude"),  # Homebrew(Apple Silicon)
        Path("/usr/local/bin/claude"),  # Homebrew(Intel)·npm 전역
        home / ".npm-global" / "bin" / "claude",
        home / ".bun" / "bin" / "claude",
    ]


def find_claude(override: str | None = None) -> Path:
    """override 경로가 있으면 그것을 쓰고, 없으면 PATH(Windows 는 .cmd/.exe 우선), 그다음 흔한
    설치 위치(`_known_locations`)에서 찾는다."""
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
    for path in _known_locations():
        if path.is_file():
            return path
    raise ClaudeNotFound(
        "PATH 와 흔한 설치 위치(~/.local/bin 등)에서 claude 실행파일을 찾을 수 없습니다. "
        "Claude Code 를 설치하거나 settings.yaml 의 claude_bin 에 경로를 지정하세요."
    )
