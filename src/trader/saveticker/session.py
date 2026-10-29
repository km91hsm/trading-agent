"""작업 간 로그인 상태 재사용과 사람이 완료하는 사이트 재확인 대기."""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from trader.platform import filelock
from trader.saveticker.browser import Browser, LoginRequired, VerificationRequired
from trader.saveticker.store import atomic_json

MANUAL_LOGIN_TIMEOUT_S = 600
LOGIN_LOCK_WAIT_S = 4 * (MANUAL_LOGIN_TIMEOUT_S + 120)
LOGIN_LOCK = "login.lock"


@contextlib.contextmanager
def login_lock(runtime: Path, *, stop: threading.Event | None = None):
    """사용자 재확인을 기다리는 다른 작업 뒤에서도 종료 신호에 반응한다."""
    stop = stop if stop is not None else threading.Event()
    deadline = time.monotonic() + LOGIN_LOCK_WAIT_S
    handle = None
    try:
        while handle is None:
            if stop.is_set():
                raise InterruptedError("login_cancelled")
            handle = filelock.try_hold(runtime / LOGIN_LOCK)
            if handle is None:
                if time.monotonic() >= deadline:
                    raise filelock.LockTimeout("login_lock_timeout")
                stop.wait(0.5)
        yield
    finally:
        filelock.release_hold(handle)


def _authenticate(adapter, runtime, *, headed=False, stop=None, on_wait=None):
    if stop is not None and stop.is_set():
        raise InterruptedError("login_cancelled")
    from playwright.sync_api import TimeoutError as BrowserTimeout

    try:
        adapter.login()
    except (LoginRequired, BrowserTimeout):
        if not headed:
            raise
        deadline = time.monotonic() + MANUAL_LOGIN_TIMEOUT_S
        adapter.stage = "waiting_verification"
        while True:
            if stop is not None and stop.is_set():
                raise InterruptedError("login_cancelled") from None
            if time.monotonic() >= deadline:
                raise VerificationRequired("verification_timeout") from None
            if on_wait is not None:
                on_wait()
            if adapter.page.is_closed():
                raise VerificationRequired("verification_window_closed") from None
            if adapter.authenticated():
                break
            # Playwright 이벤트를 처리하며 사용자 완료·종료 신호를 매초 확인한다.
            adapter.page.wait_for_timeout(1000)
    # Firebase 등의 IndexedDB 인증도 재사용한다. 로그인 실패 시 기존 state는 유지한다.
    state = adapter.page.context.storage_state(indexed_db=True)
    auth = runtime / "auth"
    auth.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_json(auth / "storage-state.json", state)  # 임시 파일 0600, 원자 교체


def serialized_login(adapter, runtime: Path, *, headed=False, stop=None, on_wait=None) -> None:
    """재로그인도 같은 잠금으로 직렬화하고 성공한 상태만 저장한다."""
    with login_lock(runtime, stop=stop):
        _authenticate(adapter, runtime, headed=headed, stop=stop, on_wait=on_wait)


@contextlib.contextmanager
def session_context(
    browser,
    runtime: Path,
    account: tuple[str, str],
    *,
    headed: bool,
    stop: threading.Event,
    on_wait: Callable[[], None],
    on_adapter: Callable[[Browser], None] | None = None,
    **adapter_options,
) -> Iterator[Browser]:
    """잠금을 잡은 뒤 최신 state를 읽고, 작업별 context를 반드시 닫는다."""
    context = None
    try:
        with login_lock(runtime, stop=stop):
            state_path = runtime / "auth" / "storage-state.json"
            context = browser.new_context(
                locale="ko-KR",
                timezone_id="Asia/Seoul",
                viewport={"width": 1440, "height": 1000},
                storage_state=str(state_path) if state_path.exists() else None,
            )
            adapter = Browser(
                context.new_page(),
                account,
                reauthenticate=lambda: serialized_login(
                    adapter,
                    runtime,
                    headed=headed,
                    stop=stop,
                    on_wait=on_wait,
                ),
                **adapter_options,
            )
            if on_adapter is not None:
                on_adapter(adapter)
            _authenticate(adapter, runtime, headed=headed, stop=stop, on_wait=on_wait)
        yield adapter
    finally:
        if context is not None:
            context.close()
