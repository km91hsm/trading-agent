"""프로세스 생존 확인 (OS 분기는 platform/ 에서만). 데몬 잠금 파일의 stale 판정에 쓴다."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable


def open_folder(path: os.PathLike[str] | str) -> None:
    """OS 파일 탐색기로 폴더를 연다 (트레이 '로그 폴더 열기')."""
    import subprocess

    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
        return
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.Popen([opener, str(path)], shell=False)


def process_start(pid: int) -> str | None:
    """프로세스 시작 시각 식별자(OS 별 문자열). pid 는 재부팅·종료 뒤 재사용되므로 잠금 파일의
    pid 가 살아 있어도 '그 데몬' 인지 확인하는 데 쓴다. 못 구하면 None(비교 생략)."""
    try:
        if sys.platform == "linux":
            with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as f:
                stat = f.read()
            # comm 에 공백·괄호가 올 수 있어 마지막 ')' 뒤를 자른다. starttime 은 그 뒤 20번째 필드
            fields = stat[stat.rindex(")") + 2 :].split()
            return fields[19]
        if sys.platform == "darwin":
            import subprocess

            out = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                timeout=5,
            )
            text = out.stdout.strip()
            return text or None
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            process_query_limited_information = 0x1000
            k32 = ctypes.windll.kernel32
            handle = k32.OpenProcess(process_query_limited_information, False, pid)
            if not handle:
                return None
            try:
                creation = wintypes.FILETIME()
                exit_t = wintypes.FILETIME()
                kernel = wintypes.FILETIME()
                user = wintypes.FILETIME()
                ok = k32.GetProcessTimes(
                    handle,
                    ctypes.byref(creation),
                    ctypes.byref(exit_t),
                    ctypes.byref(kernel),
                    ctypes.byref(user),
                )
                if not ok:
                    return None
                return str((creation.dwHighDateTime << 32) | creation.dwLowDateTime)
            finally:
                k32.CloseHandle(handle)
    except (OSError, ValueError, IndexError):
        return None
    return None


def same_process(
    pid: int, start: str | None, *, alive: Callable[[int], bool] | None = None
) -> bool:
    """pid 가 살아 있고, 시작 시각 식별자가 기록과 같으면(기록이 없으면 pid 만으로) True.
    `alive` 는 생존 판정 함수(기본 `pid_alive`; 호출자가 바꿔 끼울 수 있다)."""
    if not (alive or pid_alive)(pid):
        return False
    if not start:
        return True
    current = process_start(pid)
    return current is None or current == start


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
