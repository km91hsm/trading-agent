"""PyInstaller 번들(frozen) 분기는 이 모듈 한 곳에서만 답한다.

- 실행파일 폴더가 TRADER_HOME 이다 (config/ secrets/ state/ docs/ 하위 폴더, 폴더째 이동 가능).
- 런타임 리소스(에이전트 md·웹 dist·설정 템플릿)는 `_internal/`(sys._MEIPASS) 아래에 있다.
- 세션이 LLM 에게 허용하는 도구 명령은 frozen 이면 `trader tool`, 개발 환경이면 `python -m trader tool`.
"""

from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def executable() -> Path:
    """실행파일 경로. 번들은 실경로로(심링크 없음), 개발 환경은 venv 의 python 그대로(resolve 하면
    venv 심링크가 시스템 python 으로 풀려 PATH 앞에 둘 디렉터리가 venv bin 이 아니게 된다)."""
    exe = Path(sys.executable)
    return exe.resolve() if is_frozen() else exe


def bundle_root() -> Path:
    """실행파일 폴더(frozen) 또는 저장소 루트."""
    if is_frozen():
        return executable().parent
    from trader.platform import paths

    return paths.project_root()


def resource_dir() -> Path:
    """번들 리소스 루트: frozen 이면 `_internal/`(sys._MEIPASS), 아니면 저장소 루트."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", executable().parent / "_internal")).resolve()
    from trader.platform import paths

    return paths.project_root()


def tool_command() -> str:
    """오케스트레이터 세션의 도구 명령 접두 (`--allowedTools` 와 프롬프트가 같은 문자열을 쓴다)."""
    return "trader tool" if is_frozen() else "python -m trader tool"


def tool_bin_dir() -> Path:
    """도구 명령이 풀리도록 세션 PATH 앞에 둘 디렉터리 (실행파일 폴더 또는 venv bin)."""
    return executable().parent


def daemon_argv() -> list[str]:
    """데몬 등록(launchd·작업 스케줄러)에 쓰는 argv."""
    if is_frozen():
        return [str(executable()), "daemon"]
    return [sys.executable, "-m", "trader", "daemon"]
