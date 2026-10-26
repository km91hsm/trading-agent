"""SSE `/api/events`: 원장 카운터 스냅샷이 바뀔 때만 `change` 이벤트, 15초마다 ping.

비동기 제너레이터라 스레드 풀을 점유하지 않는다(스냅샷 조회만 run_in_threadpool).
트리거·데몬 이벤트 푸시는 Phase 6 (지금은 폴링 기반).
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import AsyncIterator

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


def snapshot(conn: sqlite3.Connection) -> dict:
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
        "decisions": _max("decisions"),
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
    }


async def stream(
    deps, *, interval_s: float = 2.0, max_events: int | None = None
) -> AsyncIterator[str]:
    conn = deps.conn_factory()  # 스트림당 연결 1개 (틱마다 열지 않는다; check_same_thread=False)
    try:
        async for item in _ticks(conn, interval_s, max_events):
            yield item
    finally:
        conn.close()


async def _ticks(conn, interval_s: float, max_events: int | None) -> AsyncIterator[str]:
    last: dict | None = None
    sent = 0
    last_ping = time.monotonic()
    while True:
        snap = await run_in_threadpool(snapshot, conn)
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
