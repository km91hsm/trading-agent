import logging
import sys

import pytest

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


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows 는 열린 파일을 rename 못 해 다른 프로세스가 먼저 회전할 수 없다",
)
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


def test_project_root_from_relative_trader_home_is_absolute(project, monkeypatch):
    """세션 cwd 는 저장소 밖 임시 디렉터리라 TRADER_HOME 이 상대경로면 tool 서브프로세스가 깨짐."""
    from trader.platform import paths

    monkeypatch.setenv("TRADER_HOME", ".")
    root = paths.project_root()
    assert root.is_absolute() and root == project.resolve()


def test_configure_with_name_uses_separate_file(project):
    """오래 사는 명령(web·telegram loop·monitor)은 데몬과 다른 파일에 쓴다 (한 파일 공유는 회전
    경쟁·뒤섞임의 원인). 기본은 trader.log."""
    logging_setup.configure(project / "state", name="web")
    logging.getLogger("trader.test").info("웹 로그")
    for h in logging.getLogger().handlers:
        h.flush()
    assert "웹 로그" in (project / "state" / "logs" / "trader-web.log").read_text(encoding="utf-8")
    assert not (project / "state" / "logs" / "trader.log").exists()


def test_rotation_permission_error_keeps_writing_same_file(project, monkeypatch):
    """Windows 에서 다른 프로세스(데몬)가 trader.log 를 쥐고 있으면 rename 이 PermissionError 다 →
    회전을 건너뛰고 같은 파일에 계속 쓰며 다음 자정에 다시 시도한다 (Logging error 소음 없음)."""
    import time

    log_dir = project / "state" / "logs"
    logging_setup.configure(project / "state")
    mine = next(h for h in logging.getLogger().handlers if hasattr(h, "rolloverAt"))
    mine.rolloverAt = int(time.time()) - 1

    def locked(src, dst):
        raise PermissionError(32, "being used by another process", src)

    monkeypatch.setattr(mine, "rotate", locked)
    errors: list = []
    monkeypatch.setattr(mine, "handleError", lambda rec: errors.append(rec))
    logging.getLogger("trader.test").info("잠긴 파일에 기록")
    mine.flush()
    assert not errors  # handleError(stderr 소음) 로 가지 않는다
    assert "잠긴 파일에 기록" in (log_dir / "trader.log").read_text(encoding="utf-8")
    assert mine.rolloverAt > int(time.time())  # 다음 회전 시각으로 미룬다
    assert not list(log_dir.glob("trader.log.*"))


def test_configure_without_stdout_skips_stream_handler(project, monkeypatch):
    """창 없는 TraderApp 은 stdout 이 None — 파일 핸들러만 붙이고 죽지 않는다."""
    import logging
    import sys

    from trader import logging_setup

    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    logging_setup.configure(project / "state")
    root = logging.getLogger()
    mine = [h for h in root.handlers if getattr(h, "name", None) == logging_setup._HANDLER_NAME]
    assert len(mine) == 1 and not any(type(h) is logging.StreamHandler for h in mine)
    logging_setup.reset()


def test_scheduler_logs_quieted_and_max_instances_dropped(tmp_path):
    """전체 검토 P10: apscheduler 실행 로그는 WARNING 이상만, max instances 경고는 버린다."""
    import logging

    from trader import logging_setup

    logging_setup.configure(tmp_path)
    try:
        lg = logging.getLogger("apscheduler.scheduler")
        assert lg.getEffectiveLevel() >= logging.WARNING
        rec = lg.makeRecord(
            lg.name,
            logging.WARNING,
            __file__,
            1,
            "Execution of job skipped: maximum number of running instances reached (1)",
            (),
            None,
        )
        assert not all(f.filter(rec) for f in lg.filters)
    finally:
        logging_setup.reset()
