"""SSE `/api/events`: 원장 카운터 스냅샷이 바뀔 때만 `change` 이벤트, 15초마다 ping.

비동기 제너레이터라 스레드 풀을 점유하지 않는다(스냅샷 조회만 run_in_threadpool).
트리거·데몬 이벤트 푸시는 Phase 6 (지금은 폴링 기반).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path

import anyio
from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from trader.control import control

router = APIRouter(tags=["events"])

PING_EVERY_S = 15.0


def _fingerprint(conn: sqlite3.Connection, table: str, *, status: bool = False) -> str:
    """테이블 지문: 건수·최대 id(+ 상태별 건수). MAX(id) 만 보면 체결·만료 같은 UPDATE 를
    못 잡는다(전체 검토 W5). 상태별 건수는 행이 많아도 GROUP BY 한 번이다."""
    row = conn.execute(f"SELECT COUNT(*) AS n, COALESCE(MAX(id), 0) AS m FROM {table}").fetchone()
    fp = f"{row['n']}:{row['m']}"
    if status:
        groups = conn.execute(
            f"SELECT status, COUNT(*) AS n FROM {table} GROUP BY status ORDER BY status"
        ).fetchall()
        fp += ":" + ",".join(f"{g['status']}={g['n']}" for g in groups)
    return fp


def _q(conn: sqlite3.Connection, sql: str) -> str:
    """지문용 한 줄 집계(행 하나의 값들을 ':' 로 잇는다). 테이블이 없으면(옛 원장) 빈 문자열."""
    try:
        row = conn.execute(sql).fetchone()
    except sqlite3.OperationalError:
        return ""
    return ":".join("" if v is None else str(v) for v in tuple(row))


# 화면이 읽지 않는 내부 커서 플래그(매 수집마다 시각이 바뀐다) — control 지문에서 뺀다
CONTROL_CURSOR_LIKE = ("news_trigger_at:%",)


def _positions_fp(conn: sqlite3.Connection) -> str:
    """잔고 지문: 행 수·최대 id + 최근 사흘(가장 늦은 날짜 기준, KR·US 운영일 차이 포함) 행의 값
    해시. 관측 시각(created_at)은 넣지 않는다."""
    head = _q(conn, "SELECT COUNT(*), MAX(id) FROM positions_snapshot")
    try:
        rows = conn.execute(
            "SELECT account_id, date, ticker, qty, avg_price, current_price, eval_amount, nav, "
            "cash FROM positions_snapshot WHERE date >= "
            "(SELECT date(MAX(date), '-3 day') FROM positions_snapshot) ORDER BY id"
        ).fetchall()
    except sqlite3.OperationalError:
        return head
    digest = hashlib.sha1(repr([tuple(r) for r in rows]).encode()).hexdigest()[:16]
    return f"{head}|{digest}"


_HB_MEMO: dict = {"key": None, "fp": ""}


def _daemon_fp(path: Path) -> str:
    """데몬 하트비트 지문: 대기열 틱이 2~5초마다 파일을 다시 쓰므로 mtime 을 그대로 쓰면 대시보드가
    몇 초마다 다시 읽는다 → 분 단위로 내린 갱신 시각 + 데몬 신원(pid·시작·종료). 기동·종료는 바로,
    잡 상태는 1분 안에 반영된다(전체 검토 3차)."""
    m = _mtime(path)
    if not m:
        return "0"
    if _HB_MEMO["key"] != (str(path), m):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        ident = f"{data.get('pid')}:{data.get('started_at')}:{data.get('stopped_at')}"
        _HB_MEMO.update(key=(str(path), m), fp=ident)
    return f"{_HB_MEMO['fp']}:{m // 60}"


def _mtime(path: Path) -> int:
    try:
        return int(path.stat().st_mtime)
    except OSError:
        return 0


def _runs_mtime(state: Path, now) -> int:
    """오늘(시장별 운영일) run 디렉터리와 그 아래 종목 디렉터리의 최신 mtime — 새 체크포인트가
    쓰이면(원자 교체로 디렉터리 mtime 이 바뀐다) 오늘 페이지가 다시 읽는다. 2단계만 훑는다."""
    from trader.orchestrator.run_store import run_date_for

    best = 0
    for m in ("KR", "US"):
        d = state / "runs" / run_date_for(m, now).isoformat() / m
        best = max(best, _mtime(d))
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.is_dir():
                        best = max(best, int(e.stat().st_mtime))
        except OSError:
            continue
    return best


# 화면이 읽는 값이 바뀌었는지 보는 지문(전체 검토 이후 '새로고침 없이 실시간' 요구). 키마다 프런트
# `tickOf(키)` 로 그 블록만 다시 읽는다. 자주 바뀌는 키(`checkpoints`·`daemon`)는 프런트가 전역
# tick 에서 뺀다(`hooks.NOISY_KEYS`).
def snapshot(conn: sqlite3.Connection, state: Path | None = None, now=None) -> dict:
    def _max(table: str) -> int:
        row = conn.execute(f"SELECT COALESCE(MAX(id), 0) AS m FROM {table}").fetchone()
        return int(row["m"])

    pending = conn.execute("SELECT COUNT(*) AS n FROM approvals WHERE status='pending'").fetchone()[
        "n"
    ]
    return {
        # 주문 표는 상태 변경(체결·만료·취소)과 체결 적재도 다시 읽어야 한다
        "orders": _fingerprint(conn, "orders", status=True) + "|" + _fingerprint(conn, "fills"),
        "approvals_pending": int(pending),
        "approvals": _fingerprint(conn, "approvals", status=True),
        "runs": _fingerprint(conn, "runs", status=True),
        # agent_calls 는 분석 세션 중 서브에이전트 호출마다 늘어 change 가 쏟아지므로 넣지 않는다
        # 같은 날 재평가는 행을 더하지 않고 json 만 바꾼다(손절·익절) → updated_at 도 본다(2-W2)
        "decisions": _q(
            conn,
            "SELECT COUNT(*) AS n, COALESCE(MAX(id), 0) AS m, COALESCE(MAX(updated_at), '') AS u "
            "FROM decisions",
        ),
        "triggers": _max("triggers"),
        "watchlist": ",".join(control.get_watchlist(conn)),
        "paused": control.is_paused(conn),
        # 작업 대기열: 새 요청·대기 수·종료분. progress 는 넣지 않는다(1초마다 change 가 나와
        # 모든 페이지가 재조회한다; 진행 문구는 활성 요청이 있을 때만 프런트가 폴링)
        "job_requests": _max("job_requests"),
        "jobs_active": conn.execute(
            "SELECT COUNT(*) AS n FROM job_requests WHERE status IN ('queued','running')"
        ).fetchone()["n"],
        "jobs_done": conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS m FROM job_requests "
            "WHERE status NOT IN ('queued','running')"
        ).fetchone()["m"],
        # 잔고 스냅샷(데몬 잔고 주기 갱신·판정·새로고침) → 순자산·보유·섹터. created_at(마지막 관측
        # 시각)은 값이 같아도 5분마다 바뀌므로 지문에서 빼고 **값**만 본다(전체 검토 3차)
        "positions": _positions_fp(conn),
        # 구독 사용률 관측 + 끝난 오케스트레이터 세션(서브에이전트 행·running 은 빼서 폭주 방지)
        "usage": _q(conn, "SELECT COUNT(*), MAX(id) FROM llm_rate_limits")
        + "|"
        + _q(
            conn,
            "SELECT COUNT(*) FROM agent_calls WHERE agent IN ('orchestrator','usage_probe',"
            "'scalp_judge') AND status != 'running'",
        ),
        # control 플래그 전체(국면·시장 뷰·운영 점검·모드·테마 상위 등). 텔레그램 오프셋은 매 폴링
        # 바뀔 수 있어 뺀다
        "control": _q(
            conn,
            "SELECT MAX(updated_at) FROM control WHERE key != 'telegram_offset' "
            f"AND {' AND '.join(f'key NOT LIKE {p!r}' for p in CONTROL_CURSOR_LIKE)}",
        ),
        "news": _q(conn, "SELECT COUNT(*), MAX(id) FROM news_items"),
        "theme": _q(conn, "SELECT COUNT(*), MAX(id), MAX(observed_at) FROM theme_heat"),
        "market_views": _q(conn, "SELECT COUNT(*), MAX(id), COUNT(matured_at) FROM market_views"),
        "shadow": _q(conn, "SELECT COUNT(*), COUNT(fwd_ret) FROM shadow_portfolios"),
        "memory": _q(conn, "SELECT COUNT(*), COUNT(outcome_at) FROM memory_entries"),
        **_file_keys(state, now),
    }


def _file_keys(state: Path | None, now) -> dict:
    """원장 밖(파일) 상태: 데몬 하트비트(잡 표·데몬 실행 중)와 오늘 체크포인트."""
    if state is None:
        return {}
    from datetime import UTC, datetime

    from trader import heartbeat

    now = now or datetime.now(UTC)
    return {
        "daemon": _daemon_fp(state / heartbeat.FILE_NAME),
        "checkpoints": _runs_mtime(state, now),
    }


SHARED_TTL_S = 1.5  # 여러 연결(탭·리더 교체)이 같은 지문을 이 시간 안에서는 한 번만 계산한다
_SHARED: dict = {"at": 0.0, "key": None, "snap": None}
_SHARED_LOCK = threading.Lock()


def shared_snapshot(conn: sqlite3.Connection, state: Path | None = None) -> dict:
    """연결이 몇 개든 2초 틱마다 원장 집계를 연결 수만큼 반복하지 않게, 최근 계산값을 나눠 쓴다
    (Codex 리뷰: 표가 커지면 탭 수에 비례한 비용)."""
    key = str(state)
    with _SHARED_LOCK:
        if _SHARED["key"] == key and time.monotonic() - _SHARED["at"] < SHARED_TTL_S:
            return _SHARED["snap"]
        snap = snapshot(conn, state)
        _SHARED.update(at=time.monotonic(), key=key, snap=snap)
        return snap


async def stream(
    deps, *, interval_s: float = 2.0, max_events: int | None = None
) -> AsyncIterator[str]:
    conn = deps.conn_factory()  # 스트림당 연결 1개 (틱마다 열지 않는다; check_same_thread=False)
    try:
        async for item in _ticks(conn, interval_s, max_events, getattr(deps, "state", None)):
            yield item
    finally:
        conn.close()


async def _ticks(
    conn, interval_s: float, max_events: int | None, state: Path | None = None
) -> AsyncIterator[str]:
    last: dict | None = None
    sent = 0
    last_ping = time.monotonic()
    while True:
        snap = await run_in_threadpool(shared_snapshot, conn, state)
        if snap != last:
            last = snap
            sent += 1
            yield f"event: change\ndata: {json.dumps(snap, ensure_ascii=False)}\n\n"
            if max_events is not None and sent >= max_events:
                return
        elif time.monotonic() - last_ping >= PING_EVERY_S:
            last_ping = time.monotonic()
            yield ": ping\n\n"
        await anyio.sleep(interval_s)


@router.get("/events")
async def events(request: Request, once: int = 0) -> StreamingResponse:
    deps = request.app.state.deps
    gen = stream(deps, max_events=1 if once else None)
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
