"""데몬 생존 판정의 로캘 차이(전체 검토 2026-10-27)."""

from trader.platform import procs


def test_legacy_localized_lock_falls_back_to_pid_alive(monkeypatch):
    monkeypatch.setattr(procs, "process_start", lambda pid: "Tue Oct 27 11:36:26 2026")
    alive = lambda pid: True  # noqa: E731
    assert procs.same_process(1, "화 10/27 11:36:26 2026", alive=alive)  # 옛 한국어 잠금
    assert not procs.same_process(1, "Mon Oct 26 09:00:00 2026", alive=alive)  # 진짜 다른 프로세스
