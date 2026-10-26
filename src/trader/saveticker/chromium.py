"""수집기용 Chromium: 번들에는 넣지 않고 첫 실행 때 Playwright 드라이버로 내려받는다
(1회, 인터넷 필요). 설치 위치는 `paths.playwright_browsers_dir()`(OS 사용자 캐시)이고
`PLAYWRIGHT_BROWSERS_PATH` 로 명시한다 — 번들(frozen)의 Playwright 는 값이 없으면 패키지 안(`0`)을
보는데 드라이버 직접 설치는 OS 캐시에 받아, 설치는 됐는데 수집 작업이 브라우저를 못 찾았다
(Codex 리뷰)."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger("trader.saveticker")

INSTALL_TIMEOUT_S = 900


def prepare_env() -> None:
    """설치·확인·수집 작업이 같은 브라우저 폴더를 보도록 env 를 고정한다(이미 있으면 존중)."""
    from trader.platform import paths

    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(paths.playwright_browsers_dir()))


def installed() -> bool:
    """Playwright 가 기대하는 Chromium 실행파일이 있는지(드라이버만 띄우고 브라우저는 열지
    않는다)."""
    from playwright.sync_api import sync_playwright

    prepare_env()
    with sync_playwright() as p:
        return Path(p.chromium.executable_path).exists()


def install(stop: threading.Event | None = None) -> bool:
    """드라이버로 `install chromium`. `stop` 이 서면(서비스 종료) 설치 프로세스를 끝내고 False —
    부모만 죽고 내려받기가 고아로 남지 않게 한다."""
    from playwright._impl._driver import compute_driver_executable, get_driver_env

    prepare_env()
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
