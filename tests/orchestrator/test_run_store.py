from datetime import date

from trader.orchestrator import run_store


def test_run_dir_layout(project):
    p = run_store.run_dir(date(2026, 10, 9), "KR", "005930")
    assert p == project / "state" / "runs" / "2026-10-09" / "KR" / "005930" and p.is_dir()


def test_write_and_read_checkpoint(project):
    p = run_store.run_dir(date(2026, 10, 9), "KR", "005930")
    run_store.write_checkpoint(p, "00_inputs", {"가": 1})
    assert (p / "00_inputs.json").read_text(encoding="utf-8").count("가") == 1
    assert run_store.read_checkpoint(p, "00_inputs") == {"가": 1}
    assert run_store.read_checkpoint(p, "05_quant") is None
    assert not list(p.glob("*.tmp"))


def test_list_checkpoints(project):
    p = run_store.run_dir(date(2026, 10, 9), "KR", "005930")
    run_store.write_checkpoint(p, "10_analyst_technical", {})
    run_store.write_checkpoint(p, "00_inputs", {})
    assert run_store.list_checkpoints(p) == ["00_inputs", "10_analyst_technical"]


def test_run_date_for_us_keeps_previous_kst_date_after_midnight():
    """US 장은 KST 자정을 넘겨 열리므로 운영일은 뉴욕 현지 날짜(= 개장 시각의 KST 날짜)다."""
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    kst = ZoneInfo("Asia/Seoul")
    # 2026-10-20 00:30 KST = 2026-10-19 11:30 ET (장중) → 운영일 2026-10-19
    assert run_store.run_date_for("US", datetime(2026, 10, 20, 0, 30, tzinfo=kst)) == date(
        2026, 10, 19
    )
    # 2026-10-19 22:00 KST = 2026-10-19 09:00 ET (개장 전) → 2026-10-19
    assert run_store.run_date_for("US", datetime(2026, 10, 19, 22, 0, tzinfo=kst)) == date(
        2026, 10, 19
    )
    # KR 은 KST 날짜 그대로
    assert run_store.run_date_for("KR", datetime(2026, 10, 20, 0, 30, tzinfo=kst)) == date(
        2026, 10, 20
    )
    # UTC 입력도 같은 결과
    assert run_store.run_date_for("US", datetime(2026, 10, 19, 15, 30, tzinfo=UTC)) == date(
        2026, 10, 19
    )
