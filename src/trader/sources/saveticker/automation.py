"""AI 호출 없는 macOS 수집 서비스 및 로컬 장애 감시."""

from __future__ import annotations

import json
import os
import plistlib
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from trader.sources.saveticker.store import atomic_json

RUNNERS = ("runner", "runner-background", "runner-community", "runner-market", "runner-news")
STREAMS = (
    "news_all",
    "community_index_live",
    "community_gain_live",
    "community_loss_live",
    "community_top5",
    "market_turnover_live",
    "market_gain_live",
    "market_loss_live",
)
LABELS = {"collector": "com.user.saveticker.collector", "monitor": "com.user.saveticker.monitor"}


def read(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def collector_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=5
    )
    return result.returncode == 0 and "trader saveticker run" in result.stdout


def evaluate(runners, status, previous, *, now, alive=collector_alive):
    counts = dict(previous.get("failure_counts", {}))
    startup = dict(previous.get("startup", {}))
    faults = []
    last = dict(previous.get("last_success_at", {}))
    for name in RUNNERS:
        row = runners.get(name)
        key = "file:" + name
        counts[key] = counts.get(key, 0) + 1 if row is None else 0
        if row is None:
            if counts[key] >= 2:
                faults.append(key)
            continue
        if not alive(row.get("pid")):
            faults.append("process:" + name)
        elif row.get("state") in ("error", "stopped") or row.get("exit_code") is not None:
            faults.append("state:" + name)
        if startup.get(name, {}).get("pid") != row.get("pid"):
            startup[name] = {"pid": row.get("pid"), "first_seen_at": now.isoformat()}
    counts["file:status"] = counts.get("file:status", 0) + 1 if status is None else 0
    if counts["file:status"] >= 2:
        faults.append("file:status")
    for name in STREAMS:
        row = (status or {}).get("streams", {}).get(name, {})
        stamp = row.get("last_success_at")
        if stamp:
            last[name] = stamp
        try:
            stale = not stamp or (now - datetime.fromisoformat(stamp)).total_seconds() > 120
        except (ValueError, TypeError):
            stale = True
        lane = (
            "news"
            if name.startswith("news")
            else "community"
            if name.startswith("community")
            else "market"
        )
        boot = startup.get("runner-" + lane, {}).get("first_seen_at")
        grace = bool(boot and (now - datetime.fromisoformat(boot)).total_seconds() < 180)
        # 이미 유효한 데이터가 있는 작업은 새 PID를 관측했다는 이유로 지연을 숨기지 않는다.
        grace = grace and (
            not stamp
            or (runners.get("runner-" + lane) or {}).get("state")
            in ("logging_in", "reauthenticating")
        )
        bad = stale or row.get("status") in ("error", "auth_required")
        key = "stale:" + name
        counts[key] = counts.get(key, 0) + 1 if bad and not grace else 0
        if counts[key] >= 2:
            faults.append(key)
    roots = [f for f in faults if not f.startswith("stale:")]
    faults = sorted(roots or faults)
    return {
        "checked_at": now.isoformat(),
        "health": "fault" if faults else "healthy",
        "faults": faults,
        "failure_counts": {k: v for k, v in counts.items() if v},
        "first_detected_at": (previous.get("first_detected_at") or now.isoformat())
        if faults
        else None,
        "startup": startup,
        "last_success_at": last,
        "last_notified_fingerprint": previous.get("last_notified_fingerprint", ""),
    }


def notify(message):
    # 메시지는 고정 문구·스트림 이름·시각만 포함한다. 셸이나 원본 예외는 사용하지 않는다.
    script = (
        "on run argv\n display notification (item 1 of argv) "
        'with title "SAVE Ticker" sound name "Glass"\nend run'
    )
    result = subprocess.run(
        ["/usr/bin/osascript", "-e", script, message], capture_output=True, text=True, timeout=15
    )
    return result.returncode == 0


def monitor(root):
    from trader.daemon import Lock

    runtime = root / "runtime"
    lock = Lock(runtime / "alert-monitor.lock")
    if not lock.acquire():
        return {"notification": "busy"}
    try:
        previous = read(runtime / "alert-monitor.json") or {}
        runners = {n: read(runtime / (n + ".json")) for n in RUNNERS}
        state = evaluate(runners, read(root / "data/status.json"), previous, now=datetime.now(UTC))
        fingerprint = "|".join(state["faults"])
        old = previous.get("last_notified_fingerprint", "")
        changed = fingerprint != old
        state["notification"] = "unchanged"
        if changed:
            stamp = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%m/%d %H:%M KST")
            last = min((v for v in state["last_success_at"].values() if v), default=None)
            last = (
                datetime.fromisoformat(last)
                .astimezone(ZoneInfo("Asia/Seoul"))
                .strftime("%m/%d %H:%M KST")
                if last
                else "미확인"
            )
            summary = (
                "수집 장애: " + ", ".join(state["faults"]) if fingerprint else "수집 복구 확인"
            )
            message = f"{stamp} · {summary} · 마지막 정상 {last}"
            try:
                delivered = notify(message)
            except (OSError, subprocess.SubprocessError):
                delivered = False
            state["notification"] = "submitted" if delivered else "failed"
            if delivered:
                state["last_notified_fingerprint"] = fingerprint
        atomic_json(runtime / "alert-monitor.json", state)
        return {
            "health": state["health"],
            "faults": state["faults"],
            "notification": state["notification"],
        }
    finally:
        lock.release()


def retry_allowed(runners):
    return not any(
        (row or {}).get("error_type") in ("AccessDenied", "LoginRequired")
        for row in runners.values()
    )


def service(root):
    from trader.daemon import Lock

    runtime = root / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    lock = Lock(runtime / "service.lock")
    if not lock.acquire():
        return
    stop = threading.Event()
    child = None
    old_handlers = {}

    def halt(*_):
        stop.set()
        if child is not None and child.poll() is None:
            child.terminate()

    for sig in (signal.SIGINT, signal.SIGTERM):
        old_handlers[sig] = signal.signal(sig, halt)
    try:
        attempts = 0
        while not stop.is_set():
            started = time.monotonic()
            with (runtime / "service.log").open("a", encoding="utf-8") as log:
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "trader",
                        "saveticker",
                        "run",
                        "--headed",
                        "--max-scrolls",
                        "2",
                        "--max-details",
                        "2",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                )
                atomic_json(
                    runtime / "service.json",
                    {
                        "pid": os.getpid(),
                        "child_pid": child.pid,
                        "state": "running",
                        "restart_count": attempts,
                        "started_at": datetime.now(UTC).isoformat(),
                    },
                )
                code = child.wait()
            if stop.is_set():
                break
            rows = {n: read(runtime / (n + ".json")) for n in RUNNERS}
            if code == 0 or not retry_allowed(rows):
                atomic_json(
                    runtime / "service.json",
                    {
                        "pid": os.getpid(),
                        "state": "blocked",
                        "reason": "authentication_or_stopped",
                        "exit_code": code,
                    },
                )
                return
            if time.monotonic() - started >= 300:
                attempts = 0
            attempts += 1
            if attempts > 3:
                atomic_json(
                    runtime / "service.json",
                    {
                        "pid": os.getpid(),
                        "state": "blocked",
                        "reason": "restart_limit",
                        "restart_count": attempts - 1,
                    },
                )
                return
            delay = 30 * 2 ** (attempts - 1)
            atomic_json(
                runtime / "service.json",
                {
                    "pid": os.getpid(),
                    "state": "backoff",
                    "restart_count": attempts,
                    "retry_after_s": delay,
                },
            )
            stop.wait(delay)
        atomic_json(runtime / "service.json", {"pid": os.getpid(), "state": "stopped"})
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            child.wait(timeout=60)
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        lock.release()


def plists(project, trader_home, *, python=sys.executable):
    runtime = trader_home / "state/external/saveticker/runtime"
    result = {}
    for kind, label in LABELS.items():
        value = {
            "Label": label,
            "ProgramArguments": [
                python,
                "-m",
                "trader",
                "saveticker",
                "service" if kind == "collector" else "monitor",
            ],
            "WorkingDirectory": str(project),
            "EnvironmentVariables": {
                "TRADER_HOME": str(trader_home),
                "PYTHONIOENCODING": "utf-8",
                "PATH": str(Path(python).parent) + ":/usr/local/bin:/usr/bin:/bin",
            },
            "RunAtLoad": True,
            "ExitTimeOut": 70,
            "StandardOutPath": str(runtime / (kind + ".launchd.out")),
            "StandardErrorPath": str(runtime / (kind + ".launchd.err")),
        }
        if kind == "monitor":
            value["StartInterval"] = 60
        result[kind] = plistlib.dumps(value)
    return result
