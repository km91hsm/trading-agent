import plistlib
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from trader.saveticker import automation


def snapshot(now):
    runners = {
        name: {"pid": index + 1, "state": "collecting", "exit_code": None}
        for index, name in enumerate(automation.RUNNERS)
    }
    status = {
        "streams": {
            name: {"count": 1, "status": "ok", "last_success_at": now.isoformat()}
            for name in automation.STREAMS
        }
    }
    return runners, status


def test_healthy_monitor_and_trends_are_ignored():
    now = datetime.now(UTC)
    r, s = snapshot(now)
    s["streams"]["community_trends"] = {"status": "error"}
    state = automation.evaluate(r, s, {}, now=now, alive=lambda pid: True)
    assert state["health"] == "healthy" and not state["faults"]


def test_dead_process_immediate_and_stale_stream_two_checks():
    now = datetime.now(UTC)
    r, s = snapshot(now)
    s["streams"]["news_all"]["last_success_at"] = (now - timedelta(seconds=150)).isoformat()
    first = automation.evaluate(r, s, {}, now=now, alive=lambda pid: True)
    assert not first["faults"]
    second = automation.evaluate(r, s, first, now=now, alive=lambda pid: True)
    assert second["faults"] == ["stale:news_all"]
    dead = automation.evaluate(r, s, {}, now=now, alive=lambda pid: False)
    assert any(x.startswith("process:") for x in dead["faults"])


def test_startup_grace_never_hides_process_death():
    now = datetime.now(UTC)
    r, s = snapshot(now)
    r["runner-news"]["state"] = "logging_in"
    s["streams"]["news_all"]["last_success_at"] = None
    first = automation.evaluate(r, s, {}, now=now, alive=lambda pid: True)
    second = automation.evaluate(
        r, s, first, now=now + timedelta(seconds=60), alive=lambda pid: True
    )
    assert not second["faults"]
    dead = automation.evaluate(r, s, first, now=now, alive=lambda pid: False)
    assert dead["faults"]


def test_auth_failure_disables_automatic_retry():
    assert automation.retry_allowed({"runner-news": {"error_type": "TimeoutError"}})
    for name in ["AccessDenied", "LoginRequired"]:
        assert not automation.retry_allowed({"runner-news": {"error_type": name}})


def test_launch_agent_runs_without_ai_or_shell_and_monitor_every_minute():
    docs = automation.plists(Path("/project"), Path("/home"), python="/python")
    collector = plistlib.loads(docs["collector"])
    monitor = plistlib.loads(docs["monitor"])
    assert collector["RunAtLoad"] and not collector.get("KeepAlive", False)
    assert collector["ProgramArguments"] == ["/python", "-m", "trader", "saveticker", "service"]
    assert monitor["StartInterval"] == 60
    assert monitor["EnvironmentVariables"]["TRADER_HOME"] == str(Path("/home"))
    assert collector["ExitTimeOut"] >= 60


def test_file_errors_require_two_checks():
    now = datetime.now(UTC)
    r, s = snapshot(now)
    first = automation.evaluate(r, None, {}, now=now, alive=lambda pid: True)
    assert not first["faults"]
    second = automation.evaluate(r, None, first, now=now, alive=lambda pid: True)
    assert "file:status" in second["faults"]


def test_monitor_notifies_fault_once_and_recovery_once(tmp_path, monkeypatch):
    from trader.saveticker.store import atomic_json

    now = datetime.now(UTC)
    runners, status = snapshot(now)
    runtime = tmp_path / "runtime"
    for name, row in runners.items():
        atomic_json(runtime / (name + ".json"), row)
    atomic_json(tmp_path / "data/status.json", status)
    sent = []
    monkeypatch.setattr(automation, "notify", lambda message: sent.append(message) or True)
    evaluate = automation.evaluate
    monkeypatch.setattr(
        automation, "evaluate", lambda *args, **kw: evaluate(*args, **kw, alive=lambda pid: False)
    )
    automation.monitor(tmp_path)
    automation.monitor(tmp_path)
    assert len(sent) == 1
    monkeypatch.setattr(
        automation, "evaluate", lambda *args, **kw: evaluate(*args, **kw, alive=lambda pid: True)
    )
    automation.monitor(tmp_path)
    automation.monitor(tmp_path)
    assert len(sent) == 2 and "복구" in sent[-1]


def test_service_transient_restart_budget_and_auth_stop(tmp_path, monkeypatch):
    from trader.saveticker.store import atomic_json

    delays = []
    launched = []

    class Stop:
        def is_set(self):
            return False

        def set(self):
            pass

        def wait(self, delay):
            delays.append(delay)

    class Child:
        pid = 12345

        def __init__(self, *args, **kwargs):
            launched.append(args)

        def wait(self, **kwargs):
            return 1

        def poll(self):
            return 1

    monkeypatch.setattr(automation.threading, "Event", Stop)
    # 전역 subprocess.Popen 을 바꾸면 daemon.Lock 의 프로세스 시작 시각 조회(ps)까지 깨진다
    fake = SimpleNamespace(**{k: getattr(subprocess, k) for k in ("DEVNULL", "SubprocessError")})
    fake.Popen = Child
    monkeypatch.setattr(automation, "subprocess", fake)
    monkeypatch.setattr("trader.saveticker.chromium.ensure", lambda stop=None: True)
    automation.service(tmp_path)
    assert len(launched) == 4 and delays == [30, 60, 120]
    state = automation.read(tmp_path / "runtime/service.json")
    assert state["reason"] == "restart_limit"
    launched.clear()
    # 세션 상실(LoginRequired)은 전체 정지 — 일부 작업의 AccessDenied 는 그 작업만 쉰다(아래 테스트)
    atomic_json(tmp_path / "runtime/runner-news.json", {"error_type": "LoginRequired"})
    automation.service(tmp_path)
    assert len(launched) == 1
    assert automation.read(tmp_path / "runtime/service.json")["reason"] == "authentication"


# --- 작업 단위 격리·자동 재시도·다시 시작 (2026-10-29 운영 점검: 커뮤니티 AccessDenied 하나가
#     뉴스·시장·캘린더까지 이틀 멈춤) -----------------------------------------------------


def test_classify_auth_failure_lane_vs_full():
    rows = {"runner": {"error_type": "WorkerExited"},
            "runner-community": {"error_type": "AccessDenied"},
            "runner-news": {"state": "stopped"}, "runner-market": {"state": "stopped"},
            "runner-background": {"state": "stopped"}}  # fmt: skip
    assert automation.classify_exit(rows) == ("lanes", {"community"})
    rows["runner-news"] = {"error_type": "LoginRequired"}
    assert automation.classify_exit(rows) == ("full", {"community", "news"})
    every = {f"runner-{x}": {"error_type": "AccessDenied"} for x in automation.LANES}
    assert automation.classify_exit(every)[0] == "full"
    assert automation.classify_exit({"runner-news": {"error_type": "TimeoutError"}}) == (
        "retry",
        set(),
    )


def test_service_skips_denied_lane_and_keeps_others(tmp_path, monkeypatch):
    from trader.saveticker.store import atomic_json

    launched = []
    runs = iter([1, 1])

    class Stop:
        n = 0

        def is_set(self):
            return Stop.n >= 2

        def set(self):
            pass

        def wait(self, delay):
            pass

    class Child:
        pid = 1

        def __init__(self, args, **kwargs):
            launched.append(args)
            Stop.n += 1
            if Stop.n == 1:  # 첫 실행: 커뮤니티 작업만 AccessDenied 로 끝남
                atomic_json(
                    tmp_path / "runtime/runner-community.json", {"error_type": "AccessDenied"}
                )

        def wait(self, **kwargs):
            return next(runs)

        def poll(self):
            return 1

    monkeypatch.setattr(automation.threading, "Event", Stop)
    fake = SimpleNamespace(**{k: getattr(subprocess, k) for k in ("DEVNULL", "SubprocessError")})
    fake.Popen = Child
    monkeypatch.setattr(automation, "subprocess", fake)
    monkeypatch.setattr("trader.saveticker.chromium.ensure", lambda stop=None: True)
    automation.service(tmp_path)
    assert len(launched) == 2
    assert "--skip-lanes" not in launched[0]
    i = launched[1].index("--skip-lanes")
    assert launched[1][i + 1] == "community"  # 두 번째 실행은 커뮤니티만 빼고 계속
    state = automation.read(tmp_path / "runtime/service.json") or {}
    assert state.get("state") != "blocked"
    blocks = automation.lane_blocks(tmp_path)
    assert set(blocks) == {"community"} and blocks["community"]["reason"] == "AccessDenied"


def test_lane_blocks_expire_and_restart_clears(tmp_path):
    from trader.saveticker.store import atomic_json

    now = datetime(2026, 10, 29, 1, 0, tzinfo=UTC)
    atomic_json(tmp_path / "runtime/lane-blocked.json",
                {"community": {"reason": "AccessDenied", "at": now.isoformat()}})  # fmt: skip
    assert automation.active_skip_lanes(tmp_path, now=now + timedelta(hours=1)) == ["community"]
    assert automation.active_skip_lanes(tmp_path, now=now + timedelta(hours=7)) == []
    blocked = {"state": "blocked", "reason": "authentication", "at": now.isoformat()}
    atomic_json(tmp_path / "runtime/service.json", blocked)
    automation.clear_blocks(tmp_path)
    assert automation.lane_blocks(tmp_path) == {}
    assert (automation.read(tmp_path / "runtime/service.json") or {}).get("state") != "blocked"


def test_status_summary_for_web(tmp_path):
    from trader.saveticker.store import atomic_json

    at = "2026-10-27T13:44:57+00:00"
    old = {"state": "blocked", "reason": "authentication_or_stopped", "at": at}
    atomic_json(tmp_path / "runtime/service.json", old)
    atomic_json(tmp_path / "runtime/runner-community.json",
                {"state": "error", "error_type": "AccessDenied", "stage": "collect_community",
                 "updated_at": "2026-10-27T13:44:42+00:00"})  # fmt: skip
    atomic_json(tmp_path / "runtime/alert-monitor.json",
                {"last_success_at": {"news_all": "2026-10-27T13:44:00+00:00",
                                     "market_gain_live": "2026-10-27T13:44:30+00:00"}})  # fmt: skip
    s = automation.status(tmp_path)
    assert s["service"]["state"] == "blocked" and s["blocked"] is True
    assert s["reason_text"].startswith("인증 실패")
    assert s["lanes"]["community"]["error_type"] == "AccessDenied"
    assert s["last_success_at"] == "2026-10-27T13:44:30+00:00"


def test_verification_required_stops_service_with_its_own_reason():
    """사이트 재확인(VerificationRequired)은 어느 작업에서든 전체 정지 — 사람이 마칠 때까지
    반복 로그인 금지."""
    rows = {
        "runner-news": {"error_type": "VerificationRequired"},
        "runner-market": {"state": "stopped"},
    }
    assert automation.classify_exit(rows) == ("verify", {"news"})
    assert not automation.retry_allowed(rows)


def test_verification_reason_text_and_retry_rule(tmp_path, monkeypatch):
    import os

    from trader.saveticker.store import atomic_json

    monkeypatch.setattr(automation, "service_elsewhere", lambda: False)
    now = datetime(2026, 10, 29, 0, 0, tzinfo=UTC)
    creds = tmp_path / "saveticker.env"
    creds.write_text("x", encoding="utf-8")
    os.utime(creds, (now.timestamp() - 60, now.timestamp() - 60))
    atomic_json(tmp_path / "runtime/service.json",
                {"state": "blocked", "reason": "verification", "at": now.isoformat()})  # fmt: skip
    assert automation.status(tmp_path)["reason_text"].startswith("사이트 재확인 필요")
    assert automation.should_start(tmp_path, creds, now=now + timedelta(hours=1))[0] is False
    assert automation.should_start(tmp_path, creds, now=now + timedelta(hours=7))[0] is True
