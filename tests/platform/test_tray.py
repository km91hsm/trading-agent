"""트레이/메뉴바: 메뉴 모델과 제목은 순수 함수(pystray 없이 검사), 상태는 원장에서 읽는다."""

import os
import signal
import sys
import threading

import pytest

from trader.platform import tray


def _state(**over):
    base = dict(paused=False, pending_approvals=0, last_error=None, web_url="http://127.0.0.1:8787")
    base.update(over)
    return tray.TrayState(**base)


def test_menu_model_order_and_labels():
    items = tray.menu_model(_state())
    assert [i.key for i in items] == ["open_web", "toggle_pause", "open_logs", "quit"]
    assert [i.label for i in items] == ["웹 UI 열기", "일시정지", "로그 폴더 열기", "종료"]
    assert all(i.enabled for i in items)


def test_menu_model_paused_shows_resume_checked():
    item = next(i for i in tray.menu_model(_state(paused=True)) if i.key == "toggle_pause")
    assert item.label == "재개" and item.checked is True


def test_title_text_priority():
    assert tray.title_text(_state()) == "trader · 실행 중"
    assert tray.title_text(_state(pending_approvals=2)) == "trader · 미승인 2"
    assert tray.title_text(_state(paused=True, pending_approvals=2)) == "trader · 일시정지"


def test_read_state_from_ledger(conn):
    from trader.control import control

    control.set_flag(conn, "paused", True)
    conn.execute(
        "INSERT INTO decisions(date, market, ticker, action, json, created_at) "
        "VALUES ('2026-10-23','KR','005930','BUY','{}','2026-10-23T00:00:00+00:00')"
    )
    did = conn.execute("SELECT id FROM decisions").fetchone()[0]
    conn.execute(
        "INSERT INTO approvals(decision_id, account_id, status, requested_at, expires_at, "
        "order_json) "
        "VALUES (?, 'paper-main', 'pending', '2026-10-23T00:00:00+00:00', "
        "'2026-10-23T00:30:00+00:00', '{}')",
        (did,),
    )
    conn.execute(
        "INSERT INTO runs(kind, market, started_at, status, error) "
        "VALUES ('KR_scan_analyze','KR','2026-10-23T00:00:00+00:00','error','세션 실패')"
    )
    conn.commit()
    st = tray.read_state(lambda: conn, "http://127.0.0.1:8787", close=False)
    assert st.paused is True and st.pending_approvals == 1 and st.last_error == "세션 실패"
    assert st.web_url == "http://127.0.0.1:8787"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX 시그널")
def test_watch_signals_delivers_while_python_handler_cannot_run():
    """트레이 모드의 메인 스레드는 AppKit 런루프에 막혀 파이썬 핸들러가 돌지 않는다 →
    wakeup fd 를 읽는 감시 스레드가 시그널 번호를 넘겨야 한다."""
    prev = signal.signal(signal.SIGTERM, lambda *_: None)
    got: list[int] = []
    seen = threading.Event()

    def on_signal(signum: int) -> None:
        got.append(signum)
        seen.set()

    release = tray.watch_signals(on_signal, (signal.SIGTERM,))
    try:
        os.kill(os.getpid(), signal.SIGTERM)
        assert seen.wait(5.0)
        assert got == [signal.SIGTERM]
    finally:
        release()
        signal.signal(signal.SIGTERM, prev)
    assert signal.set_wakeup_fd(-1) == -1  # 해제가 이전 값(-1)을 복원했다


def test_watch_signals_off_main_thread_is_noop():
    out: list = []
    t = threading.Thread(target=lambda: out.append(tray.watch_signals(lambda s: None, ())))
    t.start()
    t.join()
    out[0]()  # 예외 없이 no-op 해제


class _FakeIcon:
    def __init__(self):
        self.title = ""
        self.stopped = False
        self.menu_updates = 0

    def stop(self):
        self.stopped = True

    def update_menu(self):
        self.menu_updates += 1


def _controller(state_fn, *, on_toggle=lambda: None, on_quit=lambda: None):
    icon = _FakeIcon()
    ctl = tray.TrayController(
        state_fn=state_fn,
        on_toggle_pause=on_toggle,
        on_quit=on_quit,
        initial=state_fn(),
        run_on_main=lambda fn: fn(),
    )
    ctl.icon = icon
    return ctl, icon


def test_controller_labels_read_cached_state_without_db_calls():
    """메뉴 라벨·checked 는 캐시만 읽는다 — 메인 스레드(AppKit 런루프)에서 SQLite 를 열지 않는다."""
    calls = {"n": 0}

    def state_fn():
        calls["n"] += 1
        return _state(paused=calls["n"] > 1)

    ctl, _icon = _controller(state_fn)
    assert calls["n"] == 1
    for _ in range(10):
        ctl.label("toggle_pause")
        ctl.checked("toggle_pause")
        ctl.title()
    assert calls["n"] == 1 and ctl.label("toggle_pause") == "일시정지"
    ctl.refresh_state()
    assert calls["n"] == 2 and ctl.label("toggle_pause") == "재개" and ctl.checked("toggle_pause")


def test_quit_action_returns_immediately_and_stops_icon_after_shutdown():
    """종료는 워커 스레드에서 돈다. 메뉴를 누른 메인 스레드는 바로 돌아오고 제목이 '종료 중' 이
    되며, shutdown 이 끝난 뒤에 아이콘이 내려간다(이전엔 메인 스레드가 shutdown(wait=True) 에
    막혀 비치볼)."""
    started = threading.Event()
    release = threading.Event()

    def on_quit():
        started.set()
        release.wait(5)

    ctl, icon = _controller(lambda: _state(), on_quit=on_quit)
    ctl.quit_action(icon, None)
    assert started.wait(2)
    assert icon.title == tray.QUITTING_TITLE and icon.stopped is False
    ctl.quit_action(icon, None)  # 두 번 눌러도 한 번만
    release.set()
    for _ in range(50):
        if icon.stopped:
            break
        threading.Event().wait(0.02)
    assert icon.stopped is True


def test_pause_action_toggles_on_worker_and_refreshes_title():
    flag = {"paused": False}

    def toggle():
        flag["paused"] = not flag["paused"]

    ctl, icon = _controller(lambda: _state(paused=flag["paused"]), on_toggle=toggle)
    ctl.pause_action(icon, None)
    for _ in range(50):
        if icon.title == "trader · 일시정지":
            break
        threading.Event().wait(0.02)
    assert icon.title == "trader · 일시정지" and ctl.checked("toggle_pause") is True
    assert icon.menu_updates >= 1


def test_on_main_falls_back_to_direct_call_without_appkit():
    done = []
    tray.on_main(lambda: done.append(1))
    assert done == [1]
