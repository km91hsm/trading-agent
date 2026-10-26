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

_DATE = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")


def _old_date(text: str, cutoff: date) -> bool:
    for m in _DATE.findall(text):
        try:
            if date.fromisoformat(m) < cutoff:
                return True
        except ValueError:
            continue
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
