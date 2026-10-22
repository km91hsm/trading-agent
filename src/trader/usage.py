"""Claude 사용량: 구독 한도(5시간·7일)는 비대화형으로 읽을 공식 경로가 없다. 대화형 Claude Code 의
상태줄 스크립트가 stdin 의 `rate_limits` 를 `llm.usage_file`(기본 ~/.claude/rate_limits.json)에
남기면 그 **마지막 관측값**을 읽고, 이 저장소 몫은 원장 `agent_calls` 를 5시간·7일 창으로 집계한다."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from trader.orchestrator import tools

WINDOWS = {"h5": timedelta(hours=5), "d7": timedelta(days=7)}
_LIMIT_KEYS = ("five_hour", "seven_day", "spend_limit")


def _iso_from_epoch(v: Any) -> str | None:
    try:
        return datetime.fromtimestamp(float(v), UTC).isoformat() if v is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _window(v: Any) -> dict | None:
    if not isinstance(v, dict):
        return None
    pct = v.get("used_percentage")
    try:
        used = float(pct) if pct is not None else None
    except (TypeError, ValueError):
        used = None
    return {"used_pct": used, "resets_at": _iso_from_epoch(v.get("resets_at"))}


def read_rate_limits(path: Path, now: datetime) -> dict | None:
    """관측 파일 `{at: epoch초, rate_limits: {...}}` → 창별 사용률. 없음·깨짐·빈 값은 None."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("rate_limits"), dict):
        return None
    limits = raw["rate_limits"]
    observed = _iso_from_epoch(raw.get("at"))
    age = None
    if observed is not None:
        age = max(0, int((now - datetime.fromisoformat(observed)).total_seconds()))
    out: dict[str, Any] = {"observed_at": observed, "age_s": age}
    for k in _LIMIT_KEYS:
        out[k] = _window(limits.get(k))
    return out


def ledger_windows(conn: sqlite3.Connection, now: datetime, cap: int) -> dict:
    """agent_calls(orchestrator, fake 제외)의 5시간·7일 창: 세션 수·비용·평균 소요·오류 수."""
    out: dict[str, Any] = {}
    for key, span in WINDOWS.items():
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd), 0) AS c, AVG(duration_ms) AS d, "
            "SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS e FROM agent_calls "
            "WHERE agent='orchestrator' AND COALESCE(model, '') != 'fake' AND created_at >= ?",
            ((now - span).isoformat(),),
        ).fetchone()
        out[key] = {
            "sessions": int(row["n"]),
            "cost_usd": round(float(row["c"]), 4),
            "avg_duration_s": round(float(row["d"]) / 1000, 1) if row["d"] is not None else None,
            "errors": int(row["e"] or 0),
        }
    out["today"] = {"sessions": tools.sessions_today(conn, now), "cap": cap}
    return out


def usage_file(setting: str | None) -> Path | None:
    return Path(setting).expanduser() if setting else None
