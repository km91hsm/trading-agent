"""프로세스 생존 확인·분리 실행 (OS 분기는 platform/ 에서만). 데몬 잠금 파일의 stale 판정, 데몬이
띄우는 상시 하위 서비스(세이브티커 수집)의 기동·정리에 쓴다."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path


def open_folder(path: os.PathLike[str] | str) -> None:
    """OS 파일 탐색기로 폴더를 연다 (트레이 '로그 폴더 열기')."""
    import subprocess

    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
        return
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.Popen([opener, str(path)], shell=False)


START_FORMAT = "utc:"  # macOS process_start 값의 형식 버전(C 로캘·UTC 고정, 2026-10-27)


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

            # 로캘·시간대를 고정한다 — `lstart` 는 LANG 에 따라 '화 10/27 …' 과 'Tue Oct 27 …'
            # 처럼 달라, Trader.app(로캘 없음)이 쓴 잠금을 한국어 터미널이 '다른 프로세스' 로 보고
            # 데몬을 꺼짐으로 판정했다(전체 검토 2026-10-27). 절대경로는 최소 PATH 대비
            out = subprocess.run(
                ["/bin/ps", "-o", "lstart=", "-p", str(pid)],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                timeout=5,
                env={"LC_ALL": "C", "LANG": "C", "TZ": "UTC", "PATH": "/usr/bin:/bin"},
            )
            text = out.stdout.strip()
            # 형식 버전 접두사: 예전 잠금(로캘·시간대가 섞인 값)과 구별해 호환 비교를 한다
            return f"{START_FORMAT}{text}" if text else None
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
    if current is None or current == start:
        return True
    # 예전 macOS 잠금은 로캘·시간대가 섞인 lstart('화 10/27 …'·KST 'Tue Oct 27 11:36 …')라 새 값
    # (`utc:` 접두사, C 로캘·UTC)과 비교할 수 없다 — 그런 잠금은 pid 생존만 본다(Codex 리뷰)
    return current.startswith(START_FORMAT) and not start.startswith(START_FORMAT)


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


def spawn_detached(argv: list[str], *, log: Path, env: dict[str, str]) -> subprocess.Popen:
    """부모와 다른 프로세스 그룹(posix 새 세션, Windows 새 프로세스 그룹)으로 띄우고 출력은
    `log` 에 덧붙인다 — 데몬의 시그널·Ctrl+C 가 그대로 번지지 않게 하고, 정리는 `stop_tree` 로."""
    log.parent.mkdir(parents=True, exist_ok=True)
    kw: dict = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}  # type: ignore[attr-defined]
        if sys.platform == "win32"
        else {"start_new_session": True}
    )
    with log.open("a", encoding="utf-8") as out:
        return subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=out, stderr=out, env=env, shell=False, **kw
        )


def stop_tree(proc: subprocess.Popen, *, grace: float) -> None:
    """posix: SIGTERM(하위 정리는 그 프로세스의 핸들러 몫) → `grace` 초 안에 안 끝나면 그룹 SIGKILL.
    Windows: 그룹 신호가 없어 `taskkill /T /F` 로 트리 전체(브라우저 포함)를 끝낸다."""
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
            shell=False,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        return
    import signal

    try:
        proc.terminate()
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        proc.wait(timeout=10)
    except ProcessLookupError:
        pass


def kill_group(pid: int) -> None:
    """세션 리더 `pid` 의 프로세스 그룹(posix)을 SIGKILL, Windows 는 `taskkill /T /F` 트리."""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            shell=False,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        return
    import signal

    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
