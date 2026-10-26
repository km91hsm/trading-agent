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
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from trader import usage
from trader.agents import fake, registry
from trader.config import AccountConfig, ConfigError, Settings
from trader.ledger import db
from trader.orchestrator import tools
from trader.platform import bundle, paths
from trader.platform import claude_bin as claude_bin_mod

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
    kind: str = "analyze"  # analyze | screen | resume | reflect | market | discover
    candidates: list[dict[str, Any]] = field(default_factory=list)  # screen 모드 입력
    # screen 모드 보조 입력(Phase 21): {regime, market_view} — 국면·시장 세션 제안을 스크리너도 본다
    context: dict[str, Any] | None = None
    entries: list[dict[str, Any]] = field(default_factory=list)  # reflect 모드 입력
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
        "stream-json",  # 결과 줄 + rate_limit_event(구독 사용률) — json 결과에는 사용률이 없다
        "--verbose",  # stream-json 은 -p 에서 --verbose 가 있어야 한다
        "--model",
        model,
        "--tools",
        "Bash,Agent",
        "--allowedTools",
        f"Bash({bundle.tool_command()} *)",  # 프롬프트의 명령 문자열과 같아야 한다
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
        if spec.context:
            payload["context"] = spec.context
    if spec.kind == "resume":
        payload["trigger"] = spec.trigger
    if spec.kind == "reflect":
        payload["entries"] = spec.entries
    return json.dumps(payload, ensure_ascii=False, default=str)


def _parse_stream(stdout: str) -> tuple[dict, dict | None]:
    """claude -p 출력 → (결과 객체, 마지막 rate_limit_info | None).

    stream-json(JSONL)이면 마지막 `type == "result"` 줄과 마지막 `rate_limit_event` 를 고른다.
    단일 JSON 객체(옛 `--output-format json`·테스트 러너)도 그대로 받는다. 결과 줄이 없으면
    (중간에 죽음) PipelineError — 호출자가 세션을 error 로 닫는다."""
    text = stdout.strip()
    try:
        whole = json.loads(text)
    except ValueError:
        whole = None
    if isinstance(whole, dict):
        return whole, None
    result: dict | None = None
    rate: dict | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "result":
            result = ev
        elif ev.get("type") == "rate_limit_event" and isinstance(ev.get("rate_limit_info"), dict):
            rate = ev["rate_limit_info"]
    if result is None:
        raise PipelineError("claude 출력에 결과(result) JSON 줄이 없습니다")
    return result, rate


def _start_call(
    conn: sqlite3.Connection | None,
    run_id: int | None,
    spec: SessionSpec,
    *,
    session_id: str,
    model: str,
    cap: int | None = None,
    now: datetime | None = None,
) -> int | None:
    """세션 시작 시 running 행을 먼저 쓴다 (크래시·동시 실행 때도 상한 집계에 잡히도록).
    cap 이 있으면 같은 쓰기 잠금(`BEGIN IMMEDIATE`) 안에서 오늘 세션 수를 세어 두 프로세스가
    동시에 마지막 자리를 잡는 경합을 막는다(넘으면 PipelineError, 행 없음)."""
    if conn is None:
        return None
    conn.execute("BEGIN IMMEDIATE")
    try:
        if cap is not None and model != "fake":
            n = tools.sessions_today(conn, now or datetime.now(UTC))
            if n >= cap:
                raise PipelineError(f"오늘 세션 상한({cap})에 도달해 세션을 띄우지 않습니다")
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
    except BaseException:
        conn.rollback()
        raise
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
    num_turns: int | None = None,
) -> None:
    if conn is None:
        return
    conn.execute(
        "UPDATE agent_calls SET status=?, session_id=COALESCE(?, session_id), cost_usd=?, "
        "duration_ms=?, error=?, num_turns=? WHERE id=?",
        (status, session_id, cost, duration_ms, error, num_turns, call_id),
    )
    conn.commit()
    db.finish_run(conn, run_id, status, error)


TERM_GRACE_S = 10.0  # SIGTERM 뒤 `trader tool` 이 주문 기록을 마칠 유예
KILL_WAIT_S = 5.0
CANCEL_POLL_S = 1.0


class SessionCancelled(subprocess.TimeoutExpired):
    """종료 요청(stop_event)으로 세션을 중단했다. TimeoutExpired 의 하위라 기존 처리 경로를 탄다."""


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    if os.name == "posix":
        try:
            os.killpg(proc.pid, sig)
            return
        except ProcessLookupError:
            return
        except PermissionError:
            pass
    try:
        proc.terminate() if sig == signal.SIGTERM else proc.kill()
    except ProcessLookupError:
        pass


def _drain(proc: subprocess.Popen, timeout: float) -> bool:
    try:
        proc.communicate(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def terminate_group(proc: subprocess.Popen) -> None:
    """SIGTERM → 유예(TERM_GRACE_S) → SIGKILL → 짧은 대기 → 파이프 닫기. 어느 단계도 무한히
    기다리지 않는다(그룹을 벗어난 손자가 파이프를 쥐고 있어도 돌아온다)."""
    _signal_group(proc, signal.SIGTERM)
    if _drain(proc, TERM_GRACE_S):
        return
    _signal_group(proc, signal.SIGKILL)
    if not _drain(proc, KILL_WAIT_S):
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass


def run_claude(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess:
    """subprocess.run 과 같은 인터페이스. 새 프로세스 그룹으로 띄우고 타임아웃·취소(`cancel`
    이벤트)면 그룹을 SIGTERM → 유예 → SIGKILL 로 정리한다 — subprocess.run 의 kill 은 claude 만
    죽여 그 자식(`trader tool` 서브프로세스)이 고아로 남아 결정·주문을 계속 낼 수 있고, SIGKILL 을
    바로 보내면 주문 접수 뒤 원장 기록 전에 죽을 수 있다. Windows 는 프로세스 그룹 신호가 없어
    자식만 죽인다. 정리 대기는 유한하다(이전엔 communicate() 무한 대기로 데몬 종료가 멈출 수
    있었다)."""
    timeout = kw.pop("timeout", None)
    stdin = kw.pop("input", None)
    cancel: threading.Event | None = kw.pop("cancel", None)
    kw.pop("capture_output", None)
    posix = os.name == "posix"
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=posix,
        **kw,
    )
    deadline = None if timeout is None else time.monotonic() + float(timeout)
    pending_input = stdin
    while True:
        step = CANCEL_POLL_S if cancel is not None else None
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0:
                terminate_group(proc)
                raise subprocess.TimeoutExpired(cmd, timeout or 0)
            step = left if step is None else min(step, left)
        try:
            out, err = proc.communicate(pending_input, timeout=step)
            break
        except subprocess.TimeoutExpired:
            pending_input = None  # 첫 호출에서 stdin 은 이미 보냈다
            if cancel is not None and cancel.is_set():
                terminate_group(proc)
                raise SessionCancelled(cmd, timeout or 0) from None
            if deadline is not None and time.monotonic() >= deadline:
                terminate_group(proc)
                raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def run_session(
    spec: SessionSpec,
    settings: Settings,
    conn: sqlite3.Connection | None,
    accounts: list[AccountConfig],
    *,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
    claude_bin: Path | None = None,
    agents_dir: Path | None = None,
    state: Path | None = None,
    cancel: threading.Event | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    brokers: dict[str, Any] | None = None,
    notifier: Any = None,
) -> SessionResult:
    """오케스트레이터 세션 1회. 실패해도 체크포인트는 그대로 남아 재실행 시 이어간다."""
    if settings.anonymize:
        raise ConfigError("settings.anonymize=true 는 Phase 6 전까지 지원하지 않습니다")
    bin_override = claude_bin
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
    if not is_fake():
        ok, why = usage.session_allowance(conn, settings, kind=spec.kind, now=now())
        if not ok:
            raise PipelineError(why or "구독 사용률 감속")
    run_kind = {
        "screen": "scan",
        "discover": "scan",
        "resume": "monitor",
        "reflect": "reflect",
        "market": "market",
    }.get(spec.kind, "analyze")
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
            if spec.kind == "discover":
                written += fake.run_fake_discover(ctx, market=spec.market, date=spec.date)
            if spec.kind == "reflect":
                written += fake.run_fake_reflect(
                    ctx, market=spec.market, date=spec.date, entries=spec.entries
                )
            if spec.kind == "market":
                written += fake.run_fake_market(
                    ctx, market=spec.market, date=spec.date, debate_rounds=settings.debate_rounds
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

    cwd: Path | None = None
    try:
        # 호출자가 경로를 넘기면(테스트·dry-run) 그대로 쓰고, 아니면 settings/PATH 에서 찾는다
        bin_path = bin_override if bin_override else claude_bin_mod.find_claude(settings.claude_bin)
        # cwd 는 저장소 밖 임시 디렉터리 (claude 가 상위 CLAUDE.md·settings 를 탐색하지 못하게)
        cwd = Path(tempfile.mkdtemp(prefix="trader-claude-"))
        agents_file = cwd / "agents.json"
        names = {
            "screen": registry.SCREEN_AGENTS,
            "discover": registry.DISCOVER_AGENTS,
            "reflect": registry.REFLECT_AGENTS,
            "market": registry.MARKET_AGENTS,
        }.get(spec.kind, registry.AGENTS)
        agents_file.write_text(
            json.dumps(registry.agents_json(names, settings, agents_dir), ensure_ascii=False),
            encoding="utf-8",
        )
        prompt = registry.orchestrator_prompt(settings, agents_dir)
        session_id = str(uuid.uuid4())
        cmd = build_command(
            spec, settings, bin_path, agents_file, session_id=session_id, prompt=prompt
        )
        # 도구 명령(`python -m trader tool`·번들 `trader tool`)이 풀리도록 그 디렉터리를 PATH 앞에
        venv_bin = str(bundle.tool_bin_dir())
        env = {
            **os.environ,
            "CLAUDE_CODE_RETRY_WATCHDOG": "1",
            # tool 서브프로세스가 상속한다. "0" 일 때만 실제 주문·승인 요청 (LLM 인자 의존 없음)
            "TRADER_DRY_RUN": "1" if spec.dry_run else "0",
            "TRADER_HOME": str(paths.project_root()),
            "PATH": venv_bin + os.pathsep + os.environ.get("PATH", ""),
        }
        model = settings.models.get("orchestrator", "sonnet")
        call_id = _start_call(
            conn,
            run_id,
            spec,
            session_id=session_id,
            model=model,
            cap=settings.llm.daily_session_cap,
            now=started,
        )
    except Exception as e:
        # 기동 전 단계(실행파일·에이전트 md·프롬프트) 실패: runs 행이 running 으로 남고 임시 cwd 가
        # 누수되지 않게 닫고 지운 뒤 그대로 올린다 (호출자가 ClaudeNotFound·ConfigError 를 구분한다)
        if cwd is not None:
            shutil.rmtree(cwd, ignore_errors=True)
        if run_id is not None and conn is not None:
            db.finish_run(conn, run_id, "error", f"세션 준비 실패: {type(e).__name__}: {e}")
        raise
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
        proc = (runner or run_claude)(
            cmd,
            input=_stdin_payload(spec, asof),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(cwd),
            shell=False,
            timeout=settings.llm.timeout_s,
            **({"cancel": cancel} if cancel is not None else {}),
        )
    except SessionCancelled:
        return fail("세션 중단 (종료 요청)")
    except subprocess.TimeoutExpired:
        return fail(f"세션 타임아웃 ({settings.llm.timeout_s}s)")
    except OSError as e:  # 실행파일 없음·권한 등: running 행을 남기지 않고 오류로 닫는다
        return fail(f"세션 기동 실패: {type(e).__name__}: {e}")
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
    try:
        data, rate = _parse_stream(proc.stdout)
    except PipelineError as e:
        return fail(f"{e} (exit={proc.returncode}, stderr={proc.stderr[:200]!r})")
    if rate is not None and conn is not None:
        try:
            usage.record_rate_limit(conn, rate, now=now())
        except Exception:  # noqa: BLE001 - 사용률은 부가 정보 — 세션 결과를 막지 않는다
            logger.warning("사용률 기록 실패", exc_info=True)
    try:
        return _finish_session(conn, call_id, run_id, spec, data, proc, session_id, elapsed_ms)
    except Exception as e:  # noqa: BLE001 - 결과 처리 중 예외에도 running 행을 남기지 않는다
        logger.exception("세션 결과 처리 실패 %s", spec.market)
        return fail(f"세션 결과 처리 실패: {type(e).__name__}: {e}")


def _finish_session(
    conn, call_id, run_id, spec: SessionSpec, data: dict, proc, session_id: str, elapsed_ms
) -> SessionResult:
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
        num_turns=data.get("num_turns"),
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
