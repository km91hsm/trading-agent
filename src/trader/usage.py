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
    """원장의 창별 **최신 유효 관측**. 한 창만 담긴 이벤트(rateLimitType)가 다른 창의 최근 관측을
    지우지 않도록 창마다 따로 고르고, 창 dict 에 자기 관측 시각(`observed_at`·`age_s`)을 둔다."""
    out: dict[str, Any] = {"spend_limit": None, "source": "session", "status": None}
    newest: str | None = None
    for k in ("five_hour", "seven_day"):
        row = conn.execute(
            f"SELECT observed_at, status, source, {k}_util AS u, {k}_resets_at AS r "
            f"FROM llm_rate_limits "
            f"WHERE {k}_util IS NOT NULL ORDER BY observed_at DESC, id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            out[k] = None
            continue
        age = max(0, int((now - datetime.fromisoformat(row["observed_at"])).total_seconds()))
        out[k] = {
            "used_pct": round(float(row["u"]) * 100, 1),
            "resets_at": row["r"],
            "observed_at": row["observed_at"],
            "age_s": age,
        }
        if newest is None or row["observed_at"] > newest:
            newest, out["status"] = row["observed_at"], row["status"]
            out["source"] = row["source"] or "session"  # session | probe(데몬 사용량 확인)
    if newest is None:
        return None
    out["observed_at"] = newest
    out["age_s"] = max(0, int((now - datetime.fromisoformat(newest)).total_seconds()))
    return out


def latest_observation(
    conn: sqlite3.Connection | None, now: datetime, file_path: Path | None
) -> dict | None:
    """원장(세션 rate_limit_event)과 상태줄 파일 중 창마다 **더 최근** 관측. 모양은
    `read_rate_limits` 와 같고 창 dict 에 `observed_at`·`age_s`, 최상위에 `source`
    (가장 최근 관측의 출처) 가 붙는다. 둘 다 없으면 None."""
    cands = []
    if conn is not None:
        led = _ledger_observation(conn, now)
        if led:
            cands.append(led)
    if file_path is not None:
        f = read_rate_limits(file_path, now)
        if f:
            for k in _LIMIT_KEYS:
                if f.get(k):
                    f[k] = {**f[k], "observed_at": f["observed_at"], "age_s": f["age_s"]}
            cands.append({**f, "source": "statusline"})
    if not cands:
        return None
    big = 10**12
    best = min(cands, key=lambda c: c["age_s"] if c.get("age_s") is not None else big)
    out = dict(best)
    for k in ("five_hour", "seven_day"):
        wins = [c[k] for c in cands if c.get(k)]
        out[k] = (
            min(wins, key=lambda w: w.get("age_s") if w.get("age_s") is not None else big)
            if wins
            else None
        )
    return out


# soft 감속에서도 허용하는 보유 재평가 종류(현물 `resume`, 파생 `fo_resume` — Phase 26 R14)
RESUME_KINDS = frozenset({"resume", "fo_resume"})
# 설정 화면 켜기·끄기(bool). 없으면 yaml `llm.usage_throttle.enabled`
THROTTLE_FLAG = "usage_throttle"


def throttle_enabled(conn: sqlite3.Connection | None, settings: Any) -> tuple[bool, str]:
    """(감속 켜짐, 출처 'control'|'yaml'). 런타임 플래그가 yaml 보다 우선한다."""
    if conn is not None:
        from trader.control import control

        flag = control.get_flag(conn, THROTTLE_FLAG)
        if isinstance(flag, bool):
            return flag, "control"
    return bool(settings.llm.usage_throttle.enabled), "yaml"


def session_allowance(
    conn: sqlite3.Connection | None, settings: Any, *, kind: str, now: datetime
) -> tuple[bool, str | None]:
    """구독 사용률 감속 (Phase 24, `llm.usage_throttle`). 관측 없음·오래됨·꺼짐 → 허용.
    어느 창이든 ≥ hard → 거부, ≥ soft → 보유 재평가(`resume`)만 허용."""
    cfg = settings.llm.usage_throttle
    if not throttle_enabled(conn, settings)[0]:
        return True, None
    obs = latest_observation(conn, now, usage_file(settings.llm.usage_file))
    if obs is None:
        return True, None
    used = []
    for k, name in (("five_hour", "5시간"), ("seven_day", "7일")):
        w = obs.get(k) or {}
        age = w.get("age_s", obs.get("age_s"))
        if w.get("used_pct") is None or age is None or age > cfg.max_age_h * 3600:
            continue  # 관측 없음·오래됨
        resets = w.get("resets_at")
        if resets and datetime.fromisoformat(resets) <= now:
            continue  # 창이 이미 리셋됐다 — 리셋 전 사용률로 막지 않는다
        used.append((name, float(w["used_pct"]) / 100))
    if not used:
        return True, None
    name, top = max(used, key=lambda x: x[1])
    if top >= cfg.hard_pct:
        return (
            False,
            f"구독 {name} 사용률 {top:.0%} ≥ {cfg.hard_pct:.0%} — 새 세션을 띄우지 않습니다",
        )
    if top >= cfg.soft_pct and kind not in RESUME_KINDS:
        return False, (
            f"구독 {name} 사용률 {top:.0%} ≥ {cfg.soft_pct:.0%} — 보유 재평가만 허용({kind} 생략)"
        )
    return True, None
