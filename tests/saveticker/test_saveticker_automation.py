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
    assert monitor["EnvironmentVariables"]["TRADER_HOME"] == "/home"
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
    atomic_json(tmp_path / "runtime/runner-news.json", {"error_type": "AccessDenied"})
    automation.service(tmp_path)
    assert len(launched) == 1
