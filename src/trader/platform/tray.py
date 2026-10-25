"""트레이/메뉴바 아이콘 (Phase 15). 메뉴 모델·제목은 순수 함수, pystray 는 run_tray 안에서만
import 한다.

- macOS 는 메뉴바, Windows 는 작업표시줄 알림 영역. 메뉴: 웹 UI 열기 / 일시정지·재개 /
  로그 폴더 열기 / 종료.
- pystray 의 Icon.run() 은 **메인 스레드**에서 돌아야 한다(macOS AppKit). 데몬은
  cli.app_cmd --tray 가 백그라운드 스레드에 둔다.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from trader.control import control

logger = logging.getLogger("trader.platform.tray")


class TrayUnavailable(RuntimeError):
    """pystray(또는 macOS pyobjc)가 없어 트레이를 띄울 수 없다."""


@dataclass(frozen=True)
class TrayState:
    paused: bool
    pending_approvals: int
    last_error: str | None
    web_url: str


@dataclass(frozen=True)
class MenuItem:
    key: str
    label: str
    enabled: bool = True
    checked: bool = False


def menu_model(state: TrayState) -> list[MenuItem]:
    return [
        MenuItem("open_web", "웹 UI 열기"),
        MenuItem("toggle_pause", "재개" if state.paused else "일시정지", checked=state.paused),
        MenuItem("open_logs", "로그 폴더 열기"),
        MenuItem("quit", "종료"),
    ]


def title_text(state: TrayState) -> str:
    """툴팁/제목: 일시정지 > 미승인 n > 실행 중."""
    if state.paused:
        return "trader · 일시정지"
    if state.pending_approvals > 0:
        return f"trader · 미승인 {state.pending_approvals}"
    return "trader · 실행 중"


def read_state(
    conn_factory: Callable[[], sqlite3.Connection], web_url: str, *, close: bool = True
) -> TrayState:
    conn = conn_factory()
    try:
        paused = control.is_paused(conn)
        pending = conn.execute(
            "SELECT COUNT(*) FROM approvals WHERE status='pending'"
        ).fetchone()[0]
        row = conn.execute(
            "SELECT error FROM runs WHERE status='error' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        last_error = row[0] if row else None
    finally:
        if close:
            conn.close()
    return TrayState(
        paused=bool(paused), pending_approvals=int(pending), last_error=last_error, web_url=web_url
    )


def toggle_pause(conn_factory: Callable[[], sqlite3.Connection], *, close: bool = True) -> bool:
    """paused 플래그를 뒤집고 새 값을 돌려준다 (웹·텔레그램·CLI 와 같은 플래그)."""
    conn = conn_factory()
    try:
        new = not control.is_paused(conn)
        control.set_flag(conn, "paused", new)
        return new
    finally:
        if close:
            conn.close()


def icon_image(icon_dir: Path, *, dark_menubar: bool):
    """메뉴바 배경에 맞는 단색 ▲ (밝은 메뉴바 → 어두운 글리프)."""
    from PIL import Image

    name = "tray-light.png" if dark_menubar else "tray-dark.png"
    return Image.open(icon_dir / name)


def watch_signals(on_signal: Callable[[int], None], signals) -> Callable[[], None]:
    """메인 스레드가 AppKit 런루프(icon.run)에 막혀 있어도 시그널을 받는다.

    파이썬 핸들러는 메인 스레드가 바이트코드를 돌릴 때만 실행되므로, C 레벨 핸들러가 쓰는
    wakeup fd(소켓쌍)를 감시 스레드가 읽어 시그널 번호를 `on_signal` 로 넘긴다. 핸들러는
    호출자가 `signal.signal` 로 따로 설치해야 한다. 반환값은 해제 함수(이전 wakeup fd 복원)."""
    import signal
    import socket

    wanted = {int(s) for s in signals}
    r, w = socket.socketpair()
    w.setblocking(False)
    try:
        prev = signal.set_wakeup_fd(w.fileno(), warn_on_full_buffer=False)
    except ValueError:  # 메인 스레드가 아니면 설정할 수 없다
        r.close()
        w.close()
        return lambda: None

    def watch() -> None:
        while True:
            try:
                data = r.recv(64)
            except OSError:
                return
            if not data:
                return
            for signum in data:
                if signum in wanted:
                    try:
                        on_signal(signum)
                    except Exception:  # noqa: BLE001 - 감시 스레드는 죽지 않는다
                        logger.exception("시그널 처리 실패")

    threading.Thread(target=watch, name="trader-signal-watch", daemon=True).start()

    def release() -> None:
        try:
            signal.set_wakeup_fd(prev)
        except ValueError:
            pass
        w.close()
        try:
            r.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        r.close()

    return release


def run_tray(
    *,
    state_fn: Callable[[], TrayState],
    on_toggle_pause: Callable[[], None],
    on_open_web: Callable[[], None],
    on_open_logs: Callable[[], None],
    on_quit: Callable[[], None],
    icon_dir: Path,
    poll_s: float = 30.0,
    stop_event: threading.Event | None = None,
) -> None:
    """메인 스레드에서 아이콘을 띄우고 종료 메뉴·on_quit 까지 블로킹한다.
    돌아오면 아이콘은 내려간 뒤다."""
    try:
        import pystray
    except Exception as e:  # ImportError 외에 macOS 에서 AppKit 바인딩 오류도 온다
        raise TrayUnavailable(f"트레이를 띄울 수 없습니다: {e}") from e

    def item(key: str, label_fn, action, checked_fn=None):
        return pystray.MenuItem(label_fn, action, checked=checked_fn)

    def label(key: str):
        return lambda _item: next(i.label for i in menu_model(state_fn()) if i.key == key)

    def quit_action(icon, _item):
        try:
            on_quit()
        finally:
            icon.stop()

    def pause_action(icon, _item):
        on_toggle_pause()
        icon.title = title_text(state_fn())

    menu = pystray.Menu(
        item("open_web", label("open_web"), lambda icon, it: on_open_web()),
        item("toggle_pause", label("toggle_pause"), pause_action, lambda _it: state_fn().paused),
        item("open_logs", label("open_logs"), lambda icon, it: on_open_logs()),
        pystray.Menu.SEPARATOR,
        item("quit", label("quit"), quit_action),
    )
    icon = pystray.Icon(
        "trader", icon_image(icon_dir, dark_menubar=False), title=title_text(state_fn()), menu=menu
    )
    stop = threading.Event()

    def refresh():
        tick = min(poll_s, 1.0)
        waited = 0.0
        while not stop.wait(tick):
            if stop_event is not None and stop_event.is_set():  # 시그널 등 바깥에서 종료 요청
                icon.stop()
                return
            waited += tick
            if waited < poll_s:
                continue
            waited = 0.0
            try:
                icon.title = title_text(state_fn())
            except Exception:  # 원장 잠금 등 일시 오류는 다음 틱에
                logger.debug("트레이 상태 갱신 실패", exc_info=True)

    threading.Thread(target=refresh, name="trader-tray-refresh", daemon=True).start()
    try:
        icon.run()
    finally:
        stop.set()
