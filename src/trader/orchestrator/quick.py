"""단발 LLM 판정 (Phase 25 단계 5). 서브에이전트·도구 없이 `claude -p` 한 번으로 JSON 하나를 받는다
— 분 단위 단타 판정자(scalp-judge)처럼 분석 세션(오케스트레이터)보다 훨씬 짧고 잦은 호출용.

- `agent_calls` 행은 `agent=<에이전트 이름>`(예: scalp_judge) 이라 분석 세션 상한
  (`tools.sessions_today`, orchestrator 만 셈)에 들어가지 않고, 호출자의 `cap`(하루)이 막는다.
- 출력은 stream-json(`session._parse_stream`) — 구독 사용률(`rate_limit_event`)도 같이 남긴다.
- 실패(상한·타임아웃·비JSON·스키마 위반)는 예외 대신 None — 호출자는 '판정 없음'으로 다룬다."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from trader import usage
from trader.agents import registry, schemas
from trader.config import KST, Settings
from trader.platform import claude_bin as claude_bin_mod

logger = logging.getLogger("trader.orchestrator.quick")

BUDGET_USD = 0.2
# FAKE_CLAUDE=1: 판정자는 보수적으로 '들어가지 않음'·'유지'
_FAKE = {
    "ScalpVerdict": {
        "go": False,
        "size": "half",
        "take_profit_pct": 0.01,
        "stop_pct": 0.005,
        "max_hold_min": 30,
        "reason": "FAKE 모드: 진입하지 않음",
        "confidence": 0.5,
    },
    "ScalpAdvice": {"action": "hold", "reason": "FAKE 모드", "confidence": 0.5},
    "Briefing": {"lines": ["FAKE 모드: 아침 브리핑 없음"], "uncertain": [], "confidence": 0.5},
}
_JSON_OBJ = re.compile(r"\{.*\}", re.S)


def calls_today(conn: sqlite3.Connection, agent: str, now: datetime) -> int:
    start = now.astimezone(KST).replace(hour=0, minute=0, second=0, microsecond=0)
    row = conn.execute(
        "SELECT COUNT(*) FROM agent_calls WHERE agent=? AND COALESCE(model,'') != 'fake' "
        "AND created_at >= ?",
        (agent, start.astimezone(UTC).isoformat()),
    ).fetchone()
    return int(row[0] or 0)


def _extract_json(text: str) -> dict | None:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t[t.find("{") :] if "{" in t else t
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else None
    except ValueError:
        m = _JSON_OBJ.search(t)
        if not m:
            return None
        try:
            v = json.loads(m.group(0))
            return v if isinstance(v, dict) else None
        except ValueError:
            return None


def _spawn(
    settings: Settings,
    *,
    model: str,
    prompt: str,
    stdin: str,
    timeout_s: float,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
    claude_bin: Path | None = None,
    cancel: Any = None,
) -> subprocess.CompletedProcess:
    """도구 없는 `claude -p` 1회(stream-json). 임시 cwd·설정 차단·예산 상한은 분석 세션과 같은 원칙.
    예외(타임아웃·취소·실행 실패)는 그대로 올린다 — 호출자가 agent_calls 행을 닫는다."""
    from trader.orchestrator import session

    cwd = Path(tempfile.mkdtemp(prefix="trader-quick-"))
    try:
        bin_path = claude_bin or claude_bin_mod.find_claude(settings.claude_bin)
        cmd = [
            str(bin_path),
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            model,
            "--tools",
            "",
            "--permission-mode",
            "dontAsk",
            "--setting-sources",
            "",
            "--disable-slash-commands",
            "--append-system-prompt",
            prompt,
            "--session-id",
            str(uuid.uuid4()),
            "--max-budget-usd",
            str(BUDGET_USD),
        ]
        env = {
            **os.environ,
            "CLAUDE_CODE_RETRY_WATCHDOG": "1",
            "PATH": claude_bin_mod.child_path(Path(bin_path)),
        }
        return (runner or session.run_claude)(
            cmd,
            input=stdin,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(cwd),
            shell=False,
            timeout=timeout_s,
            **({"cancel": cancel} if cancel is not None else {}),
        )
    finally:
        shutil.rmtree(cwd, ignore_errors=True)


def ask_json(
    settings: Settings,
    conn: sqlite3.Connection,
    *,
    agent: str,
    payload: dict,
    schema: type[BaseModel],
    cap: int,
    timeout_s: float,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
    claude_bin: Path | None = None,
    agents_dir: Path | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    cancel: Any = None,
) -> BaseModel | None:
    """에이전트 md 본문(+정본 스키마)을 시스템 프롬프트로, payload JSON 을 stdin 으로 넘겨 schema 로
    검증한 결과를 돌려준다. None = 판정 없음(상한·실패). FAKE_CLAUDE=1 이면 고정 응답."""
    from trader.orchestrator import session

    call_agent = agent.replace("-", "_")
    if session.is_fake():
        return schema.model_validate(_FAKE[schema.__name__])
    started = now()
    if calls_today(conn, call_agent, started) >= cap:
        logger.info("%s 하루 상한 %d 도달 — 판정 없음", call_agent, cap)
        return None
    a = registry.load_agent(agent, agents_dir)
    model = registry.model_for(agent, settings, a.model)
    prompt = a.prompt.rstrip("\n") + "\n\n스키마: " + schemas.json_schema(schema) + "\n"
    cur = conn.execute(
        "INSERT INTO agent_calls(agent, model, status, created_at, ticker) VALUES (?,?,?,?,?)",
        (call_agent, model, "running", started.isoformat(), str(payload.get("ticker") or "")),
    )
    conn.commit()
    call_id = int(cur.lastrowid)
    t0 = time.monotonic()

    def finish(status: str, *, cost=None, error=None, sid=None) -> None:
        conn.execute(
            "UPDATE agent_calls SET status=?, cost_usd=?, duration_ms=?, error=?, session_id=? "
            "WHERE id=?",
            (status, cost, int((time.monotonic() - t0) * 1000), error, sid, call_id),
        )
        conn.commit()

    try:
        proc = _spawn(
            settings,
            model=model,
            prompt=prompt,
            stdin=json.dumps(payload, ensure_ascii=False, default=str),
            timeout_s=timeout_s,
            runner=runner,
            claude_bin=claude_bin,
            cancel=cancel,
        )
    except session.SessionCancelled:  # TimeoutExpired 의 하위라 먼저 잡는다
        finish("error", error="데몬 종료 요청으로 중단")
        return None
    except subprocess.TimeoutExpired:
        finish("error", error=f"타임아웃 ({timeout_s}s)")
        return None
    except Exception as e:  # noqa: BLE001 - 판정 실패는 '판정 없음'
        finish("error", error=f"{type(e).__name__}: {e}"[:500])
        return None
    try:
        data, rate = session._parse_stream(proc.stdout)
    except session.PipelineError as e:
        finish("error", error=str(e)[:500])
        return None
    if rate is not None:
        try:
            usage.record_rate_limit(conn, rate, now=now())
        except Exception:  # noqa: BLE001
            logger.debug("사용률 기록 실패", exc_info=True)
    cost, sid = data.get("total_cost_usd"), data.get("session_id")
    if data.get("is_error") or proc.returncode != 0:
        finish("error", cost=cost, error=str(data.get("result", ""))[:500], sid=sid)
        return None
    obj = _extract_json(str(data.get("result", "")))
    try:
        out = schema.model_validate(obj or {})
    except ValidationError as e:
        finish("error", cost=cost, error=f"스키마 위반: {e.error_count()}건", sid=sid)
        return None
    finish("ok", cost=cost, sid=sid)
    return out


PROBE_AGENT = "usage_probe"
PROBE_MODEL = "haiku"
PROBE_PROMPT = "사용량 확인용 호출입니다. 다른 말 없이 ok 한 단어만 답하세요."


def probe_usage(
    settings: Settings,
    conn: sqlite3.Connection,
    *,
    timeout_s: float = 90.0,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
    claude_bin: Path | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    cancel: Any = None,
) -> dict:
    """구독 사용률 확인(웹 실시간): 아주 짧은 `claude -p`(haiku) 1회의 `rate_limit_event` 를
    `llm_rate_limits` 에 `source='probe'` 로 남긴다. `agent_calls.agent='usage_probe'` 라 분석
    세션 상한·하루 세션 수에 들어가지 않는다. FAKE_CLAUDE=1 이면 호출 없음."""
    from trader.orchestrator import session

    if session.is_fake():
        return {"skipped": "FAKE"}
    started = now()
    cur = conn.execute(
        "INSERT INTO agent_calls(agent, model, status, created_at) VALUES (?,?,?,?)",
        (PROBE_AGENT, PROBE_MODEL, "running", started.isoformat()),
    )
    conn.commit()
    call_id, t0 = int(cur.lastrowid), time.monotonic()

    def finish(status: str, *, cost=None, error=None) -> None:
        conn.execute(
            "UPDATE agent_calls SET status=?, cost_usd=?, duration_ms=?, error=? WHERE id=?",
            (status, cost, int((time.monotonic() - t0) * 1000), error, call_id),
        )
        conn.commit()

    try:
        proc = _spawn(
            settings,
            model=PROBE_MODEL,
            prompt=PROBE_PROMPT,
            stdin="ok",
            timeout_s=timeout_s,
            runner=runner,
            claude_bin=claude_bin,
            cancel=cancel,
        )
        data, rate = session._parse_stream(proc.stdout)
    except Exception as e:  # noqa: BLE001 - 확인 실패는 다음 주기에
        finish("error", error=f"{type(e).__name__}: {e}"[:500])
        return {"error": f"{type(e).__name__}: {e}"}
    if rate is None:
        finish("error", cost=data.get("total_cost_usd"), error="rate_limit_event 없음")
        return {"error": "rate_limit_event 없음"}
    usage.record_rate_limit(conn, rate, now=now(), source="probe")
    finish("ok", cost=data.get("total_cost_usd"))
    return {"ok": True}
