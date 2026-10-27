"""외부 감시 (Phase 23 C) — 데몬 밖 프로세스가 5분마다 1회 돌며 운영 점검(`ops.health.check`)을
지금 판정해 **데몬이 죽었거나 멈춘 것·주의 이상 단계**를 텔레그램으로 알린다. 데몬이 살아 있으면
데몬의 `ops_check` 가 같은 일을 하므로 이 모듈은 데몬 생존·stale 과 그 뒤에 남은 문제만 본다.
재알림은 `state/watchdog.json` 으로 1시간 간격(단계가 더 나빠지면 즉시)."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from trader.config import AccountConfig, Settings
from trader.notify import messages
from trader.ops import health
from trader.platform import paths
from trader.platform.fsutil import atomic_replace

logger = logging.getLogger("trader.watchdog")
FILE_NAME = "watchdog.json"
RENOTIFY = timedelta(hours=1)
ALERT_LEVELS = ("warning", "critical")


def _path(state: Path | None) -> Path:
    return (state or paths.state_dir()) / FILE_NAME


def load(state: Path | None) -> dict:
    p = _path(state)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(state: Path | None, data: dict) -> None:
    p = _path(state)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
    atomic_replace(tmp, p)


DAEMON_KEYS = ("jobs.daemon", "jobs.stale")  # 데몬 사망·멈춤 — 데몬이 스스로 못 알리는 것


def should_notify(
    prev: dict, level: str, now: datetime, *, daemon_down: bool = True, new_keys: bool = False
) -> bool:
    """데몬이 죽었거나 멈췄으면 단계 격상은 즉시, 같은 단계는 1시간마다. 데몬이 살아 있으면 다른
    문제는 데몬의 `ops_check` 가 알리므로 여기서는 **새 경고 항목**이 생겼을 때만(오래 가는 공시·
    잡 오류로 1시간마다 울리던 경보 피로, 전체 검토 2026-10-27)."""
    if level not in ALERT_LEVELS:
        return False
    if not daemon_down:
        return new_keys
    last_level = str(prev.get("last_level") or "ok")
    if health.LEVELS.index(level) > health.LEVELS.index(last_level):
        return True
    last_at = prev.get("last_notified_at")
    if not last_at:
        return True
    return now - datetime.fromisoformat(last_at) >= RENOTIFY


def run_once(
    conn: sqlite3.Connection,
    settings: Settings,
    accounts: list[AccountConfig],
    notifier: Any | None,
    *,
    now: datetime | None = None,
    state: Path | None = None,
    schedule_jobs: list[Any] | None = None,
) -> dict:
    now = now or datetime.now(UTC)
    result = health.check(
        conn, settings, accounts, now=now, state=state, schedule_jobs=schedule_jobs or []
    )
    prev = load(state)
    keys = sorted(i["key"] for i in result["items"] if i["level"] in ALERT_LEVELS)
    daemon_down = any(i["key"] in DAEMON_KEYS for i in result["items"])
    new_keys = bool(set(keys) - set(prev.get("keys") or []))
    notify = notifier is not None and should_notify(
        prev, result["level"], now, daemon_down=daemon_down, new_keys=new_keys
    )
    sent = False
    if notify:
        daemon_items = [i for i in result["items"] if i["key"].startswith("jobs.")]
        head = daemon_items[0]["text"] if daemon_items else health.headline(result)
        rest = "; ".join(i["text"] for i in result["items"] if i not in daemon_items[:1])[:600]
        try:
            sent = bool(notifier.send(messages.event("감시", head, rest)))
        except Exception:  # noqa: BLE001 - 알림 실패는 다음 회차에 다시
            logger.warning("watchdog 알림 실패", exc_info=True)
    rec = {
        "last_run_at": now.isoformat(),
        "last_level": result["level"],
        "last_notified_at": now.isoformat() if sent else prev.get("last_notified_at"),
        # 알린(또는 알릴 필요가 없던) 경고 키 — 보내지 못했으면 이전 키를 유지해 다음 회차에 다시
        "keys": keys if (sent or not notify) else prev.get("keys") or [],
    }
    if result["level"] == "ok":
        rec["last_notified_at"] = None  # 정상으로 돌아오면 다음 문제는 바로 알린다
    save(state, rec)
    return {"level": result["level"], "items": len(result["items"]), "notified": sent}
