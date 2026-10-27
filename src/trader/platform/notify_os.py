"""OS 알림(창 없는 앱의 시작 실패처럼 콘솔·로그 말고는 보일 곳이 없을 때). OS 분기는 여기만."""

from __future__ import annotations

import subprocess
import sys


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def alert(title: str, message: str) -> bool:
    """알림을 띄운다(실패는 조용히 False). macOS 는 절대경로 osascript(최소 PATH 에서도 보인다),
    Windows 는 MessageBox(사용자가 닫을 때까지 기다린다 — 곧 종료하는 시작 실패에만 쓴다)."""
    try:
        if sys.platform == "darwin":
            script = f"display notification {_quote(message)} with title {_quote(title)}"
            subprocess.run(
                ["/usr/bin/osascript", "-e", script],
                shell=False,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=10,
                check=False,
            )
            return True
        if sys.platform == "win32":
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)  # MB_ICONERROR
            return True
    except (OSError, subprocess.SubprocessError, AttributeError):
        return False
    return False
