"""데몬 생존 판정의 로캘 차이(전체 검토 2026-10-27)."""

from trader.platform import procs


def test_legacy_localized_lock_falls_back_to_pid_alive(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Seoul")
    import time

    time.tzset()
    monkeypatch.setattr(procs, "process_start", lambda pid: "utc:Tue Oct 27 02:36:26 2026")
    alive = lambda pid: True  # noqa: E731
    assert procs.same_process(1, "화 10/27 11:36:26 2026", alive=alive)  # 옛 한국어·KST 잠금
    assert procs.same_process(1, "Tue Oct 27 11:36:26 2026", alive=alive)  # 옛 영어·KST 잠금
    assert not procs.same_process(1, "Tue Oct 20 11:36:26 2026", alive=alive)  # 재사용된 pid
    assert not procs.same_process(1, "utc:Mon Oct 26 09:00:00 2026", alive=alive)  # 다른 프로세스
    assert not procs.same_process(1, "알 수 없는 형식", alive=alive)


def test_process_start_returns_none_when_ps_times_out(monkeypatch):
    """2-P5: macOS `ps` 가 5초 안에 답하지 않으면 예외 대신 None (생존 판정은 pid 만으로)."""
    import subprocess

    monkeypatch.setattr(procs.sys, "platform", "darwin")

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ps", timeout=5)

    monkeypatch.setattr(subprocess, "run", boom)
    assert procs.process_start(1) is None
    assert procs.same_process(1, "utc:Tue Oct 27 02:36:26 2026", alive=lambda pid: True)
