"""Claude 사용량: 구독 한도(5시간·7일)의 관측은 두 곳이다 — (1) 세션마다 `claude -p
--output-format stream-json` 이 내는 `rate_limit_event`(Phase 24, 원장 `llm_rate_limits`),
(2) 대화형 Claude Code 상태줄이 남기는 `llm.usage_file`. 둘 중 **최신 관측**을 보여 주고
(`latest_observation`), 사용률이 높으면 새 세션을 줄인다(`session_allowance`). 이 저장소 몫은
원장 `agent_calls` 를 5시간·7일 창으로 집계한다."""

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


def _info_windows(info: dict) -> dict[str, tuple[float | None, str | None] | None]:
    """rate_limit_info → {five_hour, seven_day: (사용률 0~1, 리셋 ISO)}. `unifiedWindows` 우선,
    없으면 `rateLimitType` + `utilization` 한 창만."""
    out: dict[str, tuple[float | None, str | None] | None] = {"five_hour": None, "seven_day": None}
    unified = info.get("unifiedWindows")
    if isinstance(unified, dict):
        for k in out:
            w = unified.get(k)
            if isinstance(w, dict):
                try:
                    util = float(w["utilization"]) if w.get("utilization") is not None else None
                except (TypeError, ValueError):
                    util = None
                out[k] = (util, _iso_from_epoch(w.get("resetsAt")))
        return out
    kind = info.get("rateLimitType")
    if kind in out and info.get("utilization") is not None:
        try:
            out[kind] = (float(info["utilization"]), _iso_from_epoch(info.get("resetsAt")))
        except (TypeError, ValueError):
            pass
    return out


def record_rate_limit(
    conn: sqlite3.Connection, info: dict, *, now: datetime, source: str = "session"
) -> None:
    """세션 결과의 rate_limit_info 를 원장 `llm_rate_limits` 에 1행. 창이 없으면 쓰지 않는다."""
    w = _info_windows(info)
    if w["five_hour"] is None and w["seven_day"] is None:
        return
    fh, sd = w["five_hour"] or (None, None), w["seven_day"] or (None, None)
    conn.execute(
        "INSERT INTO llm_rate_limits(observed_at, source, status, five_hour_util, "
        "five_hour_resets_at, seven_day_util, seven_day_resets_at, raw_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            now.astimezone(UTC).isoformat(),
            source,
            str(info.get("status") or "") or None,
            fh[0],
            fh[1],
            sd[0],
            sd[1],
            json.dumps(info, ensure_ascii=False, default=str)[:4000],
        ),
    )
    conn.commit()


def _ledger_observation(conn: sqlite3.Connection, now: datetime) -> dict | None:
    row = conn.execute(
        "SELECT observed_at, status, five_hour_util, five_hour_resets_at, seven_day_util, "
        "seven_day_resets_at FROM llm_rate_limits ORDER BY observed_at DESC, id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    observed = row["observed_at"]
    age = max(0, int((now - datetime.fromisoformat(observed)).total_seconds()))

    def win(util, resets):
        if util is None:
            return None
        return {"used_pct": round(float(util) * 100, 1), "resets_at": resets}

    return {
        "observed_at": observed,
        "age_s": age,
        "five_hour": win(row["five_hour_util"], row["five_hour_resets_at"]),
        "seven_day": win(row["seven_day_util"], row["seven_day_resets_at"]),
        "spend_limit": None,
        "status": row["status"],
        "source": "session",
    }


def latest_observation(
    conn: sqlite3.Connection | None, now: datetime, file_path: Path | None
) -> dict | None:
    """원장(세션 rate_limit_event)과 상태줄 파일 중 **더 최근** 관측. 모양은 `read_rate_limits` 와
    같고 `source`(session|statusline) 가 붙는다. 둘 다 없으면 None."""
    cands = []
    if conn is not None:
        led = _ledger_observation(conn, now)
        if led:
            cands.append(led)
    if file_path is not None:
        f = read_rate_limits(file_path, now)
        if f:
            cands.append({**f, "source": "statusline"})
    if not cands:
        return None
    return min(cands, key=lambda c: c["age_s"] if c.get("age_s") is not None else 10**12)


def session_allowance(
    conn: sqlite3.Connection | None, settings: Any, *, kind: str, now: datetime
) -> tuple[bool, str | None]:
    """구독 사용률 감속 (Phase 24, `llm.usage_throttle`). 관측 없음·오래됨·꺼짐 → 허용.
    어느 창이든 ≥ hard → 거부, ≥ soft → 보유 재평가(`resume`)만 허용."""
    cfg = settings.llm.usage_throttle
    if not cfg.enabled:
        return True, None
    obs = latest_observation(conn, now, usage_file(settings.llm.usage_file))
    if obs is None or obs.get("age_s") is None or obs["age_s"] > cfg.max_age_h * 3600:
        return True, None
    used = [
        (name, (obs.get(k) or {}).get("used_pct"))
        for k, name in (("five_hour", "5시간"), ("seven_day", "7일"))
    ]
    used = [(n, u / 100) for n, u in used if u is not None]
    if not used:
        return True, None
    name, top = max(used, key=lambda x: x[1])
    if top >= cfg.hard_pct:
        return (
            False,
            f"구독 {name} 사용률 {top:.0%} ≥ {cfg.hard_pct:.0%} — 새 세션을 띄우지 않습니다",
        )
    if top >= cfg.soft_pct and kind != "resume":
        return False, (
            f"구독 {name} 사용률 {top:.0%} ≥ {cfg.soft_pct:.0%} — 보유 재평가만 허용({kind} 생략)"
        )
    return True, None
