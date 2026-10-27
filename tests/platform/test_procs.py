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
