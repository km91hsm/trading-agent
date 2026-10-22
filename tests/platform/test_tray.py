"""트레이/메뉴바: 메뉴 모델과 제목은 순수 함수(pystray 없이 검사), 상태는 원장에서 읽는다."""

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
