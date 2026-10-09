"""오케스트레이터 세션 실행 (스펙 §4.1).

`claude -p` 를 설치된 CLI 의 플래그로 띄우고 결과를 기록한다.

- 서브에이전트 정의는 `--agents <json 파일>` 로 명시 전달
  (`--setting-sources ""` 가 프로젝트 설정을 끊기 때문).
- 도구는 `--allowedTools "Bash(python -m trader tool *)"` 만. cwd 는 빈 `state/claude_cwd/`.
- `FAKE_CLAUDE=1` 이면 subprocess 없이 `agents/fake.py` 가 같은 도구 함수로 체크포인트를 쓴다.
- 세션 1회 = `agent_calls` 1행 + `runs` 1행.
  일일 세션 상한(`settings.llm.daily_session_cap`) 초과 시 PipelineError.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from trader.agents import fake, registry
from trader.config import AccountConfig, ConfigError, Settings
from trader.ledger import db
from trader.orchestrator import tools
from trader.platform import claude_bin as claude_bin_mod
from trader.platform import paths

logger = logging.getLogger("trader.orchestrator")


class PipelineError(RuntimeError):
    """세션을 띄울 수 없거나(상한·설정) 파이프라인 전제가 깨졌을 때."""


@dataclass
class SessionSpec:
    market: str
    date: date
    tickers: list[dict[str, Any]]  # [{"ticker": str, "exchange": str | None}]
    dry_run: bool = True
    asof: datetime | None = None  # tool collect 에 넘길 기준 시각 (없으면 세션 시작 시각)
    kind: str = "analyze"  # analyze | screen | resume
    candidates: list[dict[str, Any]] = field(default_factory=list)  # screen 모드 입력
    trigger: dict[str, Any] | None = None  # resume 모드 입력 (Phase 6 monitor)


@dataclass
class SessionResult:
    ok: bool
    session_id: str | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None
    num_turns: int | None = None
    error: str | None = None
    fake: bool = False
    stages_written: list[str] = field(default_factory=list)
    result_text: str | None = None


def is_fake() -> bool:
    return os.environ.get("FAKE_CLAUDE") == "1"


def build_command(
    spec: SessionSpec,
    settings: Settings,
    bin_path: Path,
    agents_file: Path,
    *,
    session_id: str,
    prompt: str,
) -> list[str]:
    """설치된 claude 2.1.x 도움말에 있는 플래그만 쓴다. --bare·--max-turns 금지."""
    model = settings.models.get("orchestrator", "sonnet")
    return [
        str(bin_path),
        "-p",
        "--output-format",
        "json",
        "--model",
        model,
        "--tools",
        "Bash,Agent",
        "--allowedTools",
        "Bash(python -m trader tool *)",
        "--permission-mode",
        "dontAsk",
        "--setting-sources",
        "",
        "--disable-slash-commands",
        "--agents",
        str(agents_file),
        "--append-system-prompt",
        prompt,
        "--session-id",
        session_id,
        "--max-budget-usd",
        str(settings.llm.budget_usd),
    ]


def _stdin_payload(spec: SessionSpec, asof: datetime) -> str:
    payload: dict[str, Any] = {
        "kind": spec.kind,
        "market": spec.market,
        "date": spec.date.isoformat(),
        "tickers": spec.tickers,
        "dry_run": spec.dry_run,
        "asof": asof.isoformat(),
    }
    if spec.kind == "screen":
        payload["candidates"] = spec.candidates
    if spec.kind == "resume":
        payload["trigger"] = spec.trigger
    return json.dumps(payload, ensure_ascii=False, default=str)


def _parse_result(stdout: str) -> dict:
    try:
        data = json.loads(stdout)
    except ValueError:
        raise PipelineError("claude 출력이 JSON 이 아닙니다") from None
    if not isinstance(data, dict):
        raise PipelineError("claude 출력이 JSON 객체가 아닙니다")
    return data


def _start_call(
    conn: sqlite3.Connection | None,
    run_id: int | None,
    spec: SessionSpec,
    *,
    session_id: str,
    model: str,
) -> int | None:
    """세션 시작 시 running 행을 먼저 쓴다 (크래시·동시 실행 때도 상한 집계에 잡히도록)."""
    if conn is None:
        return None
    cur = conn.execute(
        "INSERT INTO agent_calls(run_id, ticker, market, agent, model, session_id, status, "
        "created_at) VALUES (?, ?, ?, 'orchestrator', ?, ?, 'running', ?)",
        (
            run_id,
            ",".join(t["ticker"] for t in spec.tickers),
            spec.market,
            model,
            session_id,
            db.now_iso(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _finish_call(
    conn: sqlite3.Connection | None,
    call_id: int | None,
    run_id: int | None,
    *,
    status: str,
    session_id: str | None,
    cost: float | None,
    duration_ms: int | None,
    error: str | None,
) -> None:
    if conn is None:
        return
    conn.execute(
        "UPDATE agent_calls SET status=?, session_id=COALESCE(?, session_id), cost_usd=?, "
        "duration_ms=?, error=? WHERE id=?",
        (status, session_id, cost, duration_ms, error, call_id),
    )
    conn.commit()
    db.finish_run(conn, run_id, status, error)


def run_session(
    spec: SessionSpec,
    settings: Settings,
    conn: sqlite3.Connection | None,
    accounts: list[AccountConfig],
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    claude_bin_path: Path | None = None,
    claude_bin: Path | None = None,
    agents_dir: Path | None = None,
    state: Path | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    brokers: dict[str, Any] | None = None,
    notifier: Any = None,
) -> SessionResult:
    """오케스트레이터 세션 1회. 실패해도 체크포인트는 그대로 남아 재실행 시 이어간다."""
    if settings.anonymize:
        raise ConfigError("settings.anonymize=true 는 Phase 6 전까지 지원하지 않습니다")
    bin_override = claude_bin if claude_bin is not None else claude_bin_path
    ctx = tools.ToolContext(
        settings=settings,
        accounts=accounts,
        conn=conn,
        state=state,
        now=now,
        brokers=brokers or {},
        notifier=notifier,
        dry_run=spec.dry_run,
    )
    if tools.sessions_today(conn, now()) >= settings.llm.daily_session_cap:
        raise PipelineError(
            f"오늘 세션 상한({settings.llm.daily_session_cap})에 도달해 세션을 띄우지 않습니다"
        )
    run_kind = {"screen": "scan", "resume": "monitor"}.get(spec.kind, "analyze")
    run_id = db.start_run(conn, run_kind, spec.market) if conn is not None else None
    started = now()

    def elapsed_ms() -> int:
        return int((now() - started).total_seconds() * 1000)

    if is_fake():
        call_id = _start_call(conn, run_id, spec, session_id="fake", model=fake.FAKE_MODEL)
        written: list[str] = []
        try:
            if spec.kind == "screen":
                written += fake.run_fake_screen(
                    ctx, market=spec.market, date=spec.date, candidates=spec.candidates
                )
            for t in spec.tickers:
                written += fake.run_fake_ticker(
                    ctx,
                    ticker=t["ticker"],
                    market=spec.market,
                    date=spec.date,
                    debate_rounds=settings.debate_rounds,
                    trigger=spec.trigger,
                )
        except (tools.ToolError, ValueError) as e:
            _finish_call(
                conn,
                call_id,
                run_id,
                status="error",
                session_id="fake",
                cost=0.0,
                duration_ms=elapsed_ms(),
                error=str(e),
            )
            return SessionResult(ok=False, session_id="fake", fake=True, error=str(e))
        _finish_call(
            conn,
            call_id,
            run_id,
            status="ok",
            session_id="fake",
            cost=0.0,
            duration_ms=elapsed_ms(),
            error=None,
        )
        return SessionResult(
            ok=True,
            session_id="fake",
            cost_usd=0.0,
            duration_ms=elapsed_ms(),
            fake=True,
            stages_written=written,
        )

    # 호출자가 경로를 넘기면(테스트·dry-run) 그대로 쓰고, 아니면 settings/PATH 에서 찾는다
    bin_path = bin_override if bin_override else claude_bin_mod.find_claude(settings.claude_bin)
    # cwd 는 저장소 밖 임시 디렉터리: claude 가 상위 CLAUDE.md·settings 를 탐색하지 못하게 한다
    cwd = Path(tempfile.mkdtemp(prefix="trader-claude-"))
    agents_file = cwd / "agents.json"
    names = registry.SCREEN_AGENTS if spec.kind == "screen" else registry.AGENTS
    agents_file.write_text(
        json.dumps(registry.agents_json(names, settings, agents_dir), ensure_ascii=False),
        encoding="utf-8",
    )
    prompt = registry.orchestrator_prompt(settings, agents_dir)
    session_id = str(uuid.uuid4())
    cmd = build_command(spec, settings, bin_path, agents_file, session_id=session_id, prompt=prompt)
    # `python -m trader tool …` 이 venv 의 python 으로 풀리도록 실행파일 디렉터리를 PATH 앞에 둔다
    venv_bin = str(Path(sys.executable).parent)
    env = {
        **os.environ,
        "CLAUDE_CODE_RETRY_WATCHDOG": "1",
        # tool 서브프로세스가 상속한다. "0" 일 때만 실제 주문·승인 요청 (LLM 인자에 의존하지 않음)
        "TRADER_DRY_RUN": "1" if spec.dry_run else "0",
        "TRADER_HOME": str(paths.project_root()),
        "PATH": venv_bin + os.pathsep + os.environ.get("PATH", ""),
    }
    model = settings.models.get("orchestrator", "sonnet")
    call_id = _start_call(conn, run_id, spec, session_id=session_id, model=model)
    asof = spec.asof or started

    def fail(err: str) -> SessionResult:
        _finish_call(
            conn,
            call_id,
            run_id,
            status="error",
            session_id=session_id,
            cost=None,
            duration_ms=elapsed_ms(),
            error=err,
        )
        return SessionResult(ok=False, session_id=session_id, error=err, duration_ms=elapsed_ms())

    try:
        proc = runner(
            cmd,
            input=_stdin_payload(spec, asof),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(cwd),
            shell=False,
            timeout=settings.llm.timeout_s,
        )
    except subprocess.TimeoutExpired:
        shutil.rmtree(cwd, ignore_errors=True)
        return fail(f"세션 타임아웃 ({settings.llm.timeout_s}s)")
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
    try:
        data = _parse_result(proc.stdout)
    except PipelineError as e:
        return fail(f"{e} (exit={proc.returncode}, stderr={proc.stderr[:200]!r})")

    cost = data.get("total_cost_usd")
    duration = elapsed_ms()  # claude 의 duration_ms 는 마지막 턴 기준이라 파이썬 측 측정값을 쓴다
    sid = str(data.get("session_id") or session_id)
    is_error = bool(data.get("is_error")) or proc.returncode != 0
    result_text = str(data.get("result", ""))
    error = result_text[:500] if is_error else None
    _finish_call(
        conn,
        call_id,
        run_id,
        status="error" if is_error else "ok",
        session_id=sid,
        cost=cost,
        duration_ms=duration,
        error=error,
    )
    logger.info(
        "세션 %s %s: %s cost=%s dur=%sms",
        spec.market,
        [t["ticker"] for t in spec.tickers],
        "error" if is_error else "ok",
        cost,
        duration,
    )
    return SessionResult(
        ok=not is_error,
        session_id=sid,
        cost_usd=cost,
        duration_ms=duration,
        num_turns=data.get("num_turns"),
        error=error,
        result_text=result_text,
    )
