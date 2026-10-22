"""데몬 하트비트: `state/daemon_heartbeat.json` 하나로 데몬 생존·잡별 마지막 실행·다음 예정을 밖에서
읽게 한다. 쓰는 쪽은 데몬(`daemon.run_job`·APScheduler 리스너)뿐이고, 읽는 쪽은 `read_status`
(웹 `/api/system/daemon`·텔레그램 `/status`). 생존 판정은 `daemon.lock` 의 pid + `pid_alive`.

파일: {pid, started_at, updated_at, stopped_at?, jobs: {id: {kind, market, last_start, last_end,
last_status, last_error, next_run}}}. 쓰기 실패는 경고만(하트비트가 잡을 죽이지 않는다)."""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trader.platform.procs import pid_alive

logger = logging.getLogger("trader.heartbeat")

FILE_NAME = "daemon_heartbeat.json"
LOCK_NAME = "daemon.lock"
STALE_S = 600  # telegram_poll 30초·monitor 60초 — 10분 동안 아무 잡도 안 돌면 멈춘 것으로 본다


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


class Heartbeat:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {
            "pid": None,
            "started_at": None,
            "updated_at": None,
            "jobs": {},
        }

    def start(self, *, pid: int, started_at: datetime, jobs: list[Any]) -> None:
        with self._lock:
            self._data = {
                "pid": pid,
                "started_at": _iso(started_at),
                "updated_at": _iso(started_at),
                "jobs": {
                    j.id: {
                        "kind": j.kind,
                        "market": j.market,
                        "last_start": None,
                        "last_end": None,
                        "last_status": None,
                        "last_error": None,
                        "next_run": None,
                    }
                    for j in jobs
                },
            }
            self._write()

    def _job(self, job_id: str) -> dict[str, Any]:
        return self._data["jobs"].setdefault(
            job_id,
            {
                "kind": None,
                "market": None,
                "last_start": None,
                "last_end": None,
                "last_status": None,
                "last_error": None,
                "next_run": None,
            },
        )

    def job_started(self, job_id: str, at: datetime) -> None:
        with self._lock:
            j = self._job(job_id)
            j["last_start"] = _iso(at)
            j["last_status"] = "running"
            j["last_error"] = None
            self._touch(at)

    def job_finished(
        self, job_id: str, at: datetime, status: str, error: str | None = None
    ) -> None:
        with self._lock:
            j = self._job(job_id)
            j["last_end"] = _iso(at)
            j["last_status"] = status
            j["last_error"] = error
            self._touch(at)

    def job_missed(self, job_id: str, at: datetime) -> None:
        with self._lock:
            self._job(job_id)["last_status"] = "missed"
            self._touch(at)

    def set_next(self, job_id: str, next_at: datetime | None, *, at: datetime) -> None:
        with self._lock:
            self._job(job_id)["next_run"] = _iso(next_at) if next_at is not None else None
            self._touch(at)

    def touch(self, at: datetime) -> None:
        with self._lock:
            self._touch(at)

    def stop(self, at: datetime) -> None:
        with self._lock:
            self._data["stopped_at"] = _iso(at)
            self._touch(at)

    def _touch(self, at: datetime) -> None:
        self._data["updated_at"] = _iso(at)
        self._write()

    def _write(self) -> None:
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            logger.warning("하트비트 기록 실패 %s: %s", self.path, e)


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _secs(now: datetime, iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return max(0, int((now - datetime.fromisoformat(iso)).total_seconds()))
    except ValueError:
        return None


def read_status(state_dir: Path, now: datetime, schedule_jobs: list[Any]) -> dict:
    """잠금(pid 생존) + 하트비트 + 잡 정의를 합친다. 데몬이 없어도 잡 예정표는 나온다."""
    lock = _read_json(state_dir / LOCK_NAME) or {}
    hb = _read_json(state_dir / FILE_NAME) or {}
    lock_pid = int(lock.get("pid") or 0) or None
    alive = bool(lock_pid and pid_alive(lock_pid))
    hb_pid = hb.get("pid")
    age = _secs(now, hb.get("updated_at"))
    hb_jobs = hb.get("jobs") if isinstance(hb.get("jobs"), dict) else {}
    jobs = []
    for j in schedule_jobs:
        rec = hb_jobs.get(j.id) or {}
        jobs.append(
            {
                "id": j.id,
                "kind": j.kind,
                "market": j.market,
                "cron": j.cron,
                "interval_s": j.interval_s,
                "last_start": rec.get("last_start"),
                "last_end": rec.get("last_end"),
                "last_status": rec.get("last_status"),
                "last_error": rec.get("last_error"),
                "next_run": rec.get("next_run"),
            }
        )
    return {
        "alive": alive,
        "pid": lock_pid if alive else None,
        "started_at": lock.get("started_at") if alive else None,
        "uptime_s": _secs(now, lock.get("started_at")) if alive else None,
        "heartbeat_pid": hb_pid,
        "pid_mismatch": bool(alive and hb_pid is not None and hb_pid != lock_pid),
        "updated_at": hb.get("updated_at"),
        "age_s": age,
        "stale": bool(alive and age is not None and age > STALE_S),
        "stopped_at": hb.get("stopped_at"),
        "stale_after_s": STALE_S,
        "jobs": jobs,
    }
