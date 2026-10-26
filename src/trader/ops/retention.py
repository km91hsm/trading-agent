"""보존 정리(전체 검토 P12): 오래된 실행 산출물·하루 단위 플래그·잡 기록을 지운다.

reflect 잡 끝에서 하루 1회 돈다. 지우는 것: `state/runs/<날짜>` 디렉터리, 키에 날짜가 든 control
행(손절 기록·대조·재호가·리밸런스 같은 운영일 플래그), 끝난 `job_requests`, 세션이 없는 `runs`
(interval 잡 오류 등 — 세션 행이 있는 runs 는 readiness 운영일 집계라 남긴다), `llm_rate_limits`.
결정·주문·체결·메모리·섀도 같은 원장 사실은 지우지 않는다.
"""

from __future__ import annotations

import logging
import re
import shutil
import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path

from trader.config import KST
from trader.platform import paths

logger = logging.getLogger("trader.ops")

_DATE_SEGMENT = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# 날짜가 들어 있어도 지우지 않는 영구 키(계좌 id 에 날짜 모양이 섞여도 안전하게)
PERSISTENT_PREFIXES = frozenset(
    {"mode", "live_confirmed", "paused", "watchlist", "telegram_offset", "memory_breaker"}
)


def _old_date(key: str, cutoff: date) -> bool:
    """`:` 로 나눈 **세그먼트 하나가 통째로 날짜**인 운영일 플래그만 대상(`reconcile:<계좌>:<시장>:
    <날짜>`, `sl_recorded:…:<날짜>:<상태>` 등). 계좌 id 같은 다른 세그먼트 안의 날짜 모양은 보지
    않고 영구 키 접두사는 건너뛴다(`mode:paper-2026-01-01` 이 지워지면 계좌 모드가 풀린다)."""
    parts = key.split(":")
    if parts[0] in PERSISTENT_PREFIXES:
        return False
    # 운영일은 **마지막** 날짜 세그먼트다(계좌 id 가 날짜 모양이어도 앞쪽에 온다:
    # `sl_unrecorded:<계좌>:<시장>:<종목>:<날짜>`, `sl_recorded:…:<날짜>:<상태>`, PR #37 리뷰)
    dates = [seg for seg in parts[1:] if _DATE_SEGMENT.match(seg)]
    if not dates:
        return False
    try:
        return date.fromisoformat(dates[-1]) < cutoff
    except ValueError:
        return False


def prune(
    conn: sqlite3.Connection, state: Path | None, *, today: date, keep_days: int
) -> dict[str, int]:
    """keep_days 보다 오래된 것을 지우고 종류별 건수를 돌려준다(멱등)."""
    cutoff = today - timedelta(days=keep_days)
    cutoff_iso = datetime.combine(cutoff, time.min, KST).isoformat()
    out = {"run_dirs": 0, "control": 0, "job_requests": 0, "runs": 0, "llm_rate_limits": 0}

    root = (state or paths.state_dir()) / "runs"
    if root.exists():
        for d in sorted(root.iterdir()):
            try:
                day = date.fromisoformat(d.name)
            except ValueError:
                continue
            if d.is_dir() and day < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                out["run_dirs"] += 1

    keys = [r[0] for r in conn.execute("SELECT key FROM control").fetchall()]
    old = [k for k in keys if _old_date(k, cutoff)]
    conn.executemany("DELETE FROM control WHERE key=?", [(k,) for k in old])
    out["control"] = len(old)
    out["job_requests"] = conn.execute(
        "DELETE FROM job_requests WHERE status NOT IN ('queued','running') AND requested_at < ?",
        (cutoff_iso,),
    ).rowcount
    out["runs"] = conn.execute(
        "DELETE FROM runs WHERE started_at < ? AND NOT EXISTS ("
        "SELECT 1 FROM agent_calls a WHERE a.run_id = runs.id)",
        (cutoff_iso,),
    ).rowcount
    out["llm_rate_limits"] = conn.execute(
        "DELETE FROM llm_rate_limits WHERE observed_at < ?", (cutoff_iso,)
    ).rowcount
    conn.commit()
    if any(out.values()):
        logger.info("보존 정리(%d일 이전): %s", keep_days, out)
    return out
