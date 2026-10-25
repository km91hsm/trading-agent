"""느린 PDF/캘린더 작업과 빠른 화면 갱신을 별도 프로세스로 격리한다."""

from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path


class WorkerExited(RuntimeError):
    """하위 수집기 중 하나가 종료되어 나머지도 정지했다."""


def supervise(commands, root: Path, stop, on_status):
    processes = {}
    logs = []
    root.mkdir(parents=True, exist_ok=True)
    try:
        for lane, command in commands.items():
            if stop.is_set():
                return
            log = (root / f"{lane}.log").open("a", encoding="utf-8")
            logs.append(log)
            processes[lane] = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=os.name != "nt",
            )
        while not stop.is_set():
            workers = {}
            for lane, process in processes.items():
                state = {}
                try:
                    state = json.loads((root / f"runner-{lane}.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    pass
                workers[lane] = {**state, "pid": process.pid, "exit_code": process.poll()}
            on_status({"workers": workers})
            if any(worker["exit_code"] is not None for worker in workers.values()):
                raise WorkerExited("collector_lane_exited")
            stop.wait(1)
    finally:
        for process in processes.values():
            if process.poll() is None:
                process.terminate()
        # 공통 마감시간: 워커 수에 비례해 종료 대기가 늘어나지 않도록 한다.
        import time

        deadline = time.monotonic() + 45
        for process in processes.values():
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    process.kill()
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait()
        for log in logs:
            log.close()
