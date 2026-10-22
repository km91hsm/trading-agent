"""데몬 하트비트: state/daemon_heartbeat.json 왕복, 쓰기 실패 무시, read_status 의 alive/stale 판정."""

import json
from datetime import UTC, datetime, timedelta

from trader import heartbeat
from trader.daemon import Job

NOW = datetime(2026, 10, 23, 3, 0, tzinfo=UTC)
JOBS = [
    Job("kr_scan_analyze", "scan_analyze", "KR", cron={"hour": 7, "minute": 30}),
    Job("kr_monitor", "monitor", "KR", interval_s=60),
]


def test_heartbeat_round_trip(tmp_path):
    hb = heartbeat.Heartbeat(tmp_path / "daemon_heartbeat.json")
    hb.start(pid=4242, started_at=NOW, jobs=JOBS)
    hb.job_started("kr_monitor", NOW + timedelta(seconds=5))
    hb.job_finished("kr_monitor", NOW + timedelta(seconds=6), "ok")
    hb.job_started("kr_scan_analyze", NOW + timedelta(minutes=1))
    hb.job_finished("kr_scan_analyze", NOW + timedelta(minutes=2), "error", error="boom")
    hb.job_missed("kr_scan_analyze", NOW + timedelta(minutes=3))
    hb.set_next("kr_monitor", NOW + timedelta(minutes=4))
    data = json.loads((tmp_path / "daemon_heartbeat.json").read_text(encoding="utf-8"))
    assert data["pid"] == 4242 and data["started_at"] == NOW.isoformat()
    assert data["updated_at"] == (NOW + timedelta(minutes=4)).isoformat()
    j = data["jobs"]
    assert j["kr_monitor"]["last_status"] == "ok" and j["kr_monitor"]["kind"] == "monitor"
    assert j["kr_monitor"]["next_run"] == (NOW + timedelta(minutes=4)).isoformat()
    assert j["kr_scan_analyze"]["last_status"] == "missed"  # 놓침이 마지막 상태
    assert j["kr_scan_analyze"]["last_error"] == "boom"
    hb.stop(NOW + timedelta(minutes=5))
    data = json.loads((tmp_path / "daemon_heartbeat.json").read_text(encoding="utf-8"))
    assert data["stopped_at"] == (NOW + timedelta(minutes=5)).isoformat()


def test_heartbeat_write_failure_is_swallowed(tmp_path):
    hb = heartbeat.Heartbeat(tmp_path / "no_such_dir" / "x" / "hb.json")
    hb.start(pid=1, started_at=NOW, jobs=JOBS)  # 디렉터리가 없어도 예외 없음
    hb.job_started("kr_monitor", NOW)


def test_read_status_without_daemon_lists_schedule(tmp_path):
    st = heartbeat.read_status(tmp_path, NOW, JOBS)
    assert st["alive"] is False and st["pid"] is None and st["stale"] is False
    ids = [j["id"] for j in st["jobs"]]
    assert ids == ["kr_scan_analyze", "kr_monitor"]
    assert st["jobs"][0]["cron"] == {"hour": 7, "minute": 30} and st["jobs"][1]["interval_s"] == 60
    assert st["jobs"][1]["last_status"] is None


def test_read_status_alive_and_stale(tmp_path, monkeypatch):
    (tmp_path / "daemon.lock").write_text(
        json.dumps({"pid": 777, "started_at": (NOW - timedelta(hours=3)).isoformat()}),
        encoding="utf-8",
    )
    hb = heartbeat.Heartbeat(tmp_path / "daemon_heartbeat.json")
    hb.start(pid=777, started_at=NOW - timedelta(hours=3), jobs=JOBS)
    hb.job_started("kr_monitor", NOW - timedelta(hours=2))
    hb.job_finished("kr_monitor", NOW - timedelta(hours=2), "ok")
    monkeypatch.setattr(heartbeat, "pid_alive", lambda pid: pid == 777)
    st = heartbeat.read_status(tmp_path, NOW, JOBS)
    assert st["alive"] is True and st["pid"] == 777 and st["uptime_s"] == 3 * 3600
    assert st["age_s"] == 2 * 3600 and st["stale"] is True  # 2시간 동안 아무 잡도 안 돌았다
    assert st["jobs"][1]["last_status"] == "ok"
    # pid 가 죽으면 alive=False, 마지막 기록은 그대로
    monkeypatch.setattr(heartbeat, "pid_alive", lambda pid: False)
    st = heartbeat.read_status(tmp_path, NOW, JOBS)
    assert st["alive"] is False and st["jobs"][1]["last_status"] == "ok"
    # 하트비트의 pid 가 잠금과 다르면(옛 데몬의 파일) 잡 기록은 보여 주되 mismatch 표시
    hb2 = heartbeat.Heartbeat(tmp_path / "daemon_heartbeat.json")
    hb2.start(pid=1, started_at=NOW, jobs=JOBS)
    monkeypatch.setattr(heartbeat, "pid_alive", lambda pid: pid == 777)
    st = heartbeat.read_status(tmp_path, NOW, JOBS)
    assert st["alive"] is True and st["heartbeat_pid"] == 1 and st["pid_mismatch"] is True
