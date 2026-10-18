import logging

from trader import logging_setup


def test_configure_creates_log_and_quiets_httpx(project):
    logging_setup.configure(project / "state")
    logging.getLogger("trader.test").info("안녕 로그")
    for h in logging.getLogger().handlers:
        h.flush()
    log = project / "state" / "logs" / "trader.log"
    assert log.exists() and "안녕 로그" in log.read_text(encoding="utf-8")
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_configure_twice_no_duplicate_handlers(project):
    logging_setup.configure(project / "state")
    logging_setup.configure(project / "state")
    ours = [h for h in logging.getLogger().handlers if getattr(h, "name", "") == "trader"]
    assert len(ours) == 2  # 파일 + 콘솔


def test_reset_removes_handlers(project):
    logging_setup.configure(project / "state")
    logging_setup.reset()
    assert not [h for h in logging.getLogger().handlers if getattr(h, "name", "") == "trader"]


def test_rotation_by_another_process_reopens_stream(project):
    """두 프로세스(데몬 + web)가 같은 trader.log 를 쓸 때 한쪽이 자정 회전을 끝내면 다른 쪽은
    보관 파일에 계속 쓰지 말고 새 trader.log 를 다시 열어야 한다."""
    import time

    log_dir = project / "state" / "logs"
    logging_setup.configure(project / "state")
    mine = next(h for h in logging.getLogger().handlers if hasattr(h, "rolloverAt"))
    other = logging_setup.file_handler(log_dir / "trader.log")  # 다른 프로세스 흉내
    for h in (mine, other):
        h.rolloverAt = int(time.time()) - 1  # 둘 다 자정을 지났다고 본다
    rec = logging.LogRecord("trader.other", logging.INFO, __file__, 1, "다른 프로세스", None, None)
    other.emit(rec)  # 다른 프로세스가 먼저 회전한다
    other.close()
    archives = list(log_dir.glob("trader.log.*"))
    assert len(archives) == 1
    logging.getLogger("trader.test").info("회전 뒤 기록")
    mine.flush()
    assert "회전 뒤 기록" in (log_dir / "trader.log").read_text(encoding="utf-8")
    assert "회전 뒤 기록" not in archives[0].read_text(encoding="utf-8")
