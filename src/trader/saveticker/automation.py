"""AI 호출 없는 수집 서비스 및 로컬 장애 감시.

서비스는 데몬(`daemon._saveticker_body`, 앱이 켜져 있는 동안)이나 macOS launchd 가 띄운다. 같은
수집 폴더의 `runtime/service.lock` 으로 하나만 돌고, 인증 실패·재기동 한도로 멈추면
`should_start` 가 다시 띄울 때를 정한다."""

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

from trader.platform import bundle
from trader.saveticker.store import atomic_json

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
    if os.name == "nt":  # ps 가 없다 — pid 생존만 본다
        from trader.platform import procs

        return procs.pid_alive(pid)
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


def monitor(root, *, send=None):
    """`send(message) -> bool`: 알림 전송(기본 macOS 알림 센터, 데몬은 텔레그램)."""
    send = send or notify
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
                delivered = send(message)
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


LANES = ("background", "community", "market", "news")
AUTH_ERRORS = ("AccessDenied", "LoginRequired", "VerificationRequired")
LANE_BLOCK_FILE = "lane-blocked.json"
LANE_RETRY_S = 6 * 3600  # 접근이 막힌 작업만 쉬고 이 시간 뒤 1회 다시 넣는다
AUTH_RETRY_S = 6 * 3600  # 인증 실패 전체 정지도 계정 파일 변경 없이 이 간격으로만 다시 시도


def classify_exit(runners):
    """수집 실행이 끝난 이유 → ("verify"|"full"|"lanes"|"retry", 인증 실패 작업들).
    사이트 재확인(VerificationRequired)이 하나라도 있으면 "verify"(사람이 마칠 때까지 정지),
    LoginRequired(세션 상실)이 있거나 모든 작업이 AccessDenied 면 전체 정지("full"),
    일부 작업만 AccessDenied 면 그 작업만 쉰다("lanes"), 인증 실패가 없으면 일반 재시도("retry")."""
    denied: dict[str, str] = {}
    for name, row in runners.items():
        err = (row or {}).get("error_type")
        if err in AUTH_ERRORS and name.startswith("runner-"):
            denied[name.removeprefix("runner-")] = err
    if not denied:
        return "retry", set()
    lanes = set(denied)
    if "VerificationRequired" in denied.values():
        return "verify", lanes
    if "LoginRequired" in denied.values() or lanes >= set(LANES):
        return "full", lanes
    return "lanes", lanes


def lane_blocks(home):
    return read(home / "runtime" / LANE_BLOCK_FILE) or {}


def active_skip_lanes(home, *, now):
    """쉬는 중인 작업(막힌 지 LANE_RETRY_S 안). 시간이 지나면 다시 넣는다."""
    out = []
    for lane, row in sorted(lane_blocks(home).items()):
        try:
            at = datetime.fromisoformat(row["at"])
        except (KeyError, TypeError, ValueError):
            continue
        if (now - at).total_seconds() < LANE_RETRY_S:
            out.append(lane)
    return out


def _block_lanes(home, lanes, runners, *, now):
    blocks = lane_blocks(home)
    for lane in lanes:
        err = (runners.get(f"runner-{lane}") or {}).get("error_type")
        blocks[lane] = {"reason": err or "AccessDenied", "at": now.isoformat()}
    atomic_json(home / "runtime" / LANE_BLOCK_FILE, blocks)


def clear_blocks(home):
    """사람의 '다시 시작': 작업 쉬기·서비스 정지 기록을 지워 데몬이 1분 안에 다시 띄우게 한다."""
    runtime = home / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / LANE_BLOCK_FILE).unlink(missing_ok=True)
    state = read(runtime / "service.json") or {}
    if state.get("state") == "blocked":
        atomic_json(
            runtime / "service.json",
            {"state": "stopped", "cleared_at": datetime.now(UTC).isoformat()},
        )


_REASON_TEXT = {
    "verification": (
        "사이트 재확인 필요 — 브라우저에서 세이브티커에 로그인해 재확인을 마친 뒤 '다시 시작'"
        "(6시간 뒤 1회 자동 재시도)"
    ),
    "authentication": "인증 실패로 멈춤 — 6시간마다 다시 시도(계정 확인 필요할 수 있음)",
    "authentication_or_stopped": "인증 실패로 멈춤 — 6시간마다 다시 시도(계정 확인 필요할 수 있음)",
    "restart_limit": "재기동 한도로 멈춤 — 1시간 뒤 다시 시도",
    "chromium_install_failed": "Chromium 설치 실패 — 1시간 뒤 다시 시도",
}


def status(home):
    """웹·운영 점검용 요약: 서비스 상태·멈춘 이유·작업별 상태·쉬는 작업·마지막 수집 시각."""
    runtime = home / "runtime"
    service_state = read(runtime / "service.json") or {}
    lanes = {}
    for lane in LANES:
        row = read(runtime / f"runner-{lane}.json") or {}
        lanes[lane] = {k: row.get(k) for k in ("state", "error_type", "stage", "updated_at")}
    monitor = read(runtime / "alert-monitor.json") or {}
    success = monitor.get("last_success_at") or {}
    last = (
        max((v for v in success.values() if isinstance(v, str)), default=None)
        if isinstance(success, dict)
        else None
    )
    blocked = service_state.get("state") == "blocked"
    return {
        "service": {k: service_state.get(k) for k in ("state", "reason", "at", "exit_code")},
        "blocked": blocked,
        "reason_text": _REASON_TEXT.get(service_state.get("reason"), service_state.get("reason"))
        if blocked
        else None,
        "lanes": lanes,
        "lane_blocks": lane_blocks(home),
        "last_success_at": last,
        "health": monitor.get("health"),
    }


def retry_allowed(runners):
    return not any((row or {}).get("error_type") in AUTH_ERRORS for row in runners.values())


STOP_GRACE_S = 70  # 데몬 종료 때 서비스(→ 수집 작업 → 브라우저) 정리 유예
RESTART_LIMIT_RETRY = 3600  # 재기동 한도로 멈춘 뒤 다시 띄우기까지(초)


def _blocked(runtime, reason, **extra):
    atomic_json(
        runtime / "service.json",
        {
            "pid": os.getpid(),
            "state": "blocked",
            "reason": reason,
            "at": datetime.now(UTC).isoformat(),
            **extra,
        },
    )


def kill_leftovers(root):
    """서비스가 죽은 뒤 남은 수집 작업(각자 세션 리더라 서비스 그룹 SIGKILL 이 닿지 않는다)과
    그 브라우저를 끝낸다. `runner-<lane>.json` 의 pid·시작 시각이 지금 프로세스와 같고 수집기
    명령이면 그 그룹을 SIGKILL.
    끝낸 수를 돌려준다."""
    from trader.platform import procs

    killed = 0
    for name in RUNNERS:
        row = read(root / "runtime" / (name + ".json")) or {}
        pid, start = row.get("pid"), row.get("proc_start")
        if not isinstance(pid, int) or pid == os.getpid() or not start:
            continue  # 시작 시각 기록이 없으면 그 프로세스라고 확인할 수 없다 — 건드리지 않는다
        # pid 는 재부팅 뒤 다른 프로그램에 재사용된다(Windows 는 명령줄 확인도 없다)
        if procs.process_start(pid) == start and collector_alive(pid):
            procs.kill_group(pid)
            killed += 1
    return killed


def service_elsewhere():
    """이 PC 에서 (다른 수집 폴더라도) 수집 서비스가 이미 도는지 — 같은 계정으로 두 곳에서
    로그인하면 사이트가 계정을 막을 수 있다. posix 는 `ps`, Windows 는 확인하지 않는다(False)."""
    if os.name == "nt":
        return False
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return False
    me = os.getpid()
    for line in result.stdout.splitlines():
        pid, _, command = line.strip().partition(" ")
        if pid.isdigit() and int(pid) != me and "trader saveticker service" in command:
            return True
    return False


def should_start(home, credentials, *, now):
    """데몬이 서비스를 (다시) 띄울지와 이유. 계정 파일은 있는지만 본다(값은 읽지 않는다).
    인증 실패로 멈췄으면 계정 파일이 그 뒤에 바뀌어야, 재기동 한도·Chromium 설치 실패는 1시간
    뒤에 다시 띄운다 — 실패한 로그인을 1분마다 반복하면 계정이 잠길 수 있다."""
    from trader.daemon import Lock

    if not credentials.is_file():
        return False, "계정 파일 없음"
    runtime = home / "runtime"
    if Lock(runtime / "service.lock").held():
        return False, "이미 실행 중"
    if service_elsewhere():
        return False, "다른 폴더의 수집 서비스가 실행 중"
    state = read(runtime / "service.json") or {}
    if state.get("state") != "blocked":
        return True, ""
    try:
        at = datetime.fromisoformat(state["at"])
    except (KeyError, TypeError, ValueError):
        at = None
    if state.get("reason") in ("authentication", "authentication_or_stopped", "verification"):
        changed = at is None or credentials.stat().st_mtime > at.timestamp()
        waited = at is not None and (now - at).total_seconds() >= AUTH_RETRY_S
        if changed or waited:
            return True, ""
        return False, "인증 실패로 멈춤 — 6시간 뒤 다시 시도(계정 파일을 바꾸면 바로)"
    if at is not None and (now - at).total_seconds() < RESTART_LIMIT_RETRY:
        return False, f"멈춤({state.get('reason')}) — 1시간 뒤 재시도"
    return True, ""


def service(root):
    from trader.daemon import Lock

    runtime = root / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    lock = Lock(runtime / "service.lock")
    if not lock.acquire():
        return
    from trader.saveticker import chromium

    # 설치·확인·수집 작업(자식은 env 를 물려받는다)이 같은 브라우저 폴더를 본다
    chromium.prepare_env()
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
        # 종료 핸들러를 건 뒤 설치한다 — 설치 중 종료되면 설치 프로세스도 함께 끝낸다(Codex 리뷰)
        if not chromium.ensure(stop):
            if not stop.is_set():
                _blocked(runtime, "chromium_install_failed")
            return
        attempts = 0
        while not stop.is_set():
            started = time.monotonic()
            skip = active_skip_lanes(root, now=datetime.now(UTC))
            with (runtime / "service.log").open("a", encoding="utf-8") as log:
                child = subprocess.Popen(
                    [
                        *bundle.cli_argv(),
                        "saveticker",
                        "run",
                        "--headed",
                        "--max-scrolls",
                        "2",
                        "--max-details",
                        "2",
                        *(["--skip-lanes", ",".join(skip)] if skip else []),
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
            kind, lanes = classify_exit(rows)
            if kind == "verify":
                _blocked(runtime, "verification", exit_code=code, lanes=sorted(lanes))
                return
            if kind == "full":
                _blocked(runtime, "authentication", exit_code=code, lanes=sorted(lanes))
                return
            if kind == "lanes":
                # 일부 작업만 접근이 막혔다 — 그 작업만 쉬고 나머지는 바로 다시 띄운다(2026-10-29)
                _block_lanes(root, lanes, rows, now=datetime.now(UTC))
                for lane in lanes:
                    atomic_json(
                        runtime / f"runner-{lane}.json",
                        {"state": "paused", "reason": "access_denied"},
                    )
                continue
            if time.monotonic() - started >= 300:
                attempts = 0
            attempts += 1
            if attempts > 3:
                _blocked(runtime, "restart_limit", restart_count=attempts - 1)
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
    """macOS launchd 등록용(데몬 없이 상시 수집할 때). 앱을 쓰면 데몬이 서비스를 띄우므로
    필요 없다."""
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
