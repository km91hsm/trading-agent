"""수집기용 Chromium: 번들에는 넣지 않고 첫 실행 때 Playwright 드라이버로 내려받는다
(1회, 인터넷 필요). 설치 위치는 Playwright 기본 캐시(macOS `~/Library/Caches/ms-playwright`,
Windows `%LOCALAPPDATA%\\ms-playwright`)."""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger("trader.saveticker")

INSTALL_TIMEOUT_S = 900


def installed() -> bool:
    """Playwright 가 기대하는 Chromium 실행파일이 있는지(드라이버만 띄우고 브라우저는 열지
    않는다)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        return Path(p.chromium.executable_path).exists()


def install(stop: threading.Event | None = None) -> bool:
    """드라이버로 `install chromium`. `stop` 이 서면(서비스 종료) 설치 프로세스를 끝내고 False —
    부모만 죽고 내려받기가 고아로 남지 않게 한다."""
    from playwright._impl._driver import compute_driver_executable, get_driver_env

    driver = compute_driver_executable()
    argv = [*driver] if isinstance(driver, tuple) else [str(driver)]
    logger.info("세이브티커 수집기용 Chromium 설치 시작(1회)")
    proc = subprocess.Popen(
        [*argv, "install", "chromium"],
        env=get_driver_env(),
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + INSTALL_TIMEOUT_S
    while True:
        try:
            code = proc.wait(timeout=1)
            break
        except subprocess.TimeoutExpired:
            if (stop is not None and stop.is_set()) or time.monotonic() > deadline:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                return False
    if code != 0:
        logger.warning("Chromium 설치 실패 (exit %s)", code)
    return code == 0


def ensure(stop: threading.Event | None = None) -> bool:
    """없으면 설치. 이미 있거나 설치에 성공하면 True."""
    try:
        return installed() or install(stop)
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("Chromium 확인·설치 실패: %s", type(e).__name__)
        return False
