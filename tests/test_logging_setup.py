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
